#!/usr/bin/env python3
"""RAM-only CP900 wake-word bridge from Qwen ASR to OpenClaw and Audio8."""

from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import re
import signal
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import deque
from dataclasses import dataclass
from typing import Any


SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
WAKE_WORDS = frozenset({"cerberus", "cerebrus"})
TRIM_AFTER_WAKE = " \t\r\n,.:;!?—–-"
MAX_SPOKEN_CHARACTERS = 2_000
MAX_TTS_CHUNKS = 16
TTS_CHUNK_CHARACTERS = 140


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A loopback dependency must never redirect a private request elsewhere."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


# Do not honor HTTP(S)_PROXY for microphone transcripts or synthesized audio.
_EMPTY_PROXY_HANDLER = urllib.request.ProxyHandler({})
_LOCAL_HTTP_OPENER = urllib.request.build_opener(
    _EMPTY_PROXY_HANDLER,
    _NoRedirectHandler(),
)


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a number") from error
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def require_loopback_http_url(name: str, value: str) -> str:
    """Only allow explicit local HTTP dependencies; never proxy mic text away."""
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError(f"{name} must be an ordinary loopback HTTP URL")
    if parsed.hostname != "localhost":
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as error:
            raise RuntimeError(f"{name} hostname must be localhost or loopback IP") from error
        if not address.is_loopback:
            raise RuntimeError(f"{name} must resolve explicitly to loopback")
    try:
        port = parsed.port
    except ValueError as error:
        raise RuntimeError(f"{name} has an invalid port") from error
    if port is not None and not 1 <= port <= 65_535:
        raise RuntimeError(f"{name} has an invalid port")
    if not parsed.path.startswith("/"):
        raise RuntimeError(f"{name} must include an absolute path")
    return value


@dataclass(frozen=True)
class Settings:
    asr_url: str
    openclaw_url: str
    openclaw_token: str
    openclaw_model: str
    openclaw_user: str
    tts_url: str
    tts_model: str
    capture_device: str
    playback_device: str
    frame_ms: int
    pre_roll_ms: int
    speech_start_ms: int
    trailing_silence_ms: int
    minimum_voice_ms: int
    maximum_utterance_seconds: float
    vad_minimum_rms: float
    vad_noise_ratio: float
    armed_seconds: float
    playback_cooldown_seconds: float
    asr_timeout_seconds: float
    openclaw_timeout_seconds: float
    tts_timeout_seconds: float
    log_transcripts: bool

    @classmethod
    def from_environment(cls) -> "Settings":
        asr_url = require_loopback_http_url(
            "VOICE_ASR_URL",
            os.environ.get("VOICE_ASR_URL", "http://127.0.0.1:8020/transcribe"),
        )
        openclaw_url = require_loopback_http_url(
            "VOICE_OPENCLAW_URL",
            os.environ.get(
                "VOICE_OPENCLAW_URL",
                "http://127.0.0.1:18789/v1/chat/completions",
            ),
        )
        tts_url = require_loopback_http_url(
            "VOICE_TTS_URL",
            os.environ.get("VOICE_TTS_URL", "http://127.0.0.1:8010/v1/audio/speech"),
        )
        frame_ms = env_int("VOICE_FRAME_MS", 20, 10, 100)
        if 1000 % frame_ms:
            raise RuntimeError("VOICE_FRAME_MS must divide evenly into 1000")
        model = os.environ.get("VOICE_OPENCLAW_MODEL", "openclaw/voice").strip()
        user = os.environ.get("VOICE_OPENCLAW_USER", "cerebrus3-voice").strip()
        if not model or len(model) > 200:
            raise RuntimeError("VOICE_OPENCLAW_MODEL must contain 1-200 characters")
        if not user or len(user) > 200:
            raise RuntimeError("VOICE_OPENCLAW_USER must contain 1-200 characters")
        return cls(
            asr_url=asr_url,
            openclaw_url=openclaw_url,
            openclaw_token=os.environ.get("VOICE_OPENCLAW_TOKEN", "").strip(),
            openclaw_model=model,
            openclaw_user=user,
            tts_url=tts_url,
            tts_model=os.environ.get("VOICE_TTS_MODEL", "audio8/tts-0.6b").strip(),
            capture_device=os.environ.get(
                "VOICE_CAPTURE_DEVICE", "plughw:CARD=CP900,DEV=0"
            ),
            playback_device=os.environ.get(
                "VOICE_PLAYBACK_DEVICE", "plughw:CARD=CP900,DEV=0"
            ),
            frame_ms=frame_ms,
            pre_roll_ms=env_int("VOICE_PRE_ROLL_MS", 300, 0, 2000),
            speech_start_ms=env_int("VOICE_SPEECH_START_MS", 80, 20, 1000),
            trailing_silence_ms=env_int(
                "VOICE_TRAILING_SILENCE_MS", 700, 100, 3000
            ),
            minimum_voice_ms=env_int("VOICE_MINIMUM_VOICE_MS", 180, 20, 3000),
            maximum_utterance_seconds=env_float(
                "VOICE_MAX_UTTERANCE_SECONDS", 30, 2, 35
            ),
            vad_minimum_rms=env_float("VOICE_VAD_MINIMUM_RMS", 350, 1, 20_000),
            vad_noise_ratio=env_float("VOICE_VAD_NOISE_RATIO", 3.0, 1.1, 20),
            armed_seconds=env_float("VOICE_ARMED_SECONDS", 12, 2, 60),
            playback_cooldown_seconds=env_float(
                "VOICE_PLAYBACK_COOLDOWN_SECONDS", 1.0, 0.1, 10
            ),
            asr_timeout_seconds=env_float("VOICE_ASR_TIMEOUT_SECONDS", 120, 5, 600),
            openclaw_timeout_seconds=env_float(
                "VOICE_OPENCLAW_TIMEOUT_SECONDS", 900, 10, 3600
            ),
            tts_timeout_seconds=env_float("VOICE_TTS_TIMEOUT_SECONDS", 240, 10, 900),
            log_transcripts=os.environ.get("VOICE_LOG_TRANSCRIPTS", "0") == "1",
        )


def pcm16_rms(frame: bytes) -> float:
    if not frame or len(frame) % SAMPLE_WIDTH:
        raise ValueError("PCM frame must contain complete 16-bit samples")
    count = len(frame) // SAMPLE_WIDTH
    squares = sum(sample * sample for (sample,) in struct.iter_unpack("<h", frame))
    return math.sqrt(squares / count)


class EnergyVad:
    """Small adaptive energy VAD with pre-roll and bounded utterance memory."""

    def __init__(self, settings: Settings) -> None:
        self.frame_ms = settings.frame_ms
        self.pre_roll_frames = max(1, settings.pre_roll_ms // self.frame_ms)
        self.start_frames = max(1, settings.speech_start_ms // self.frame_ms)
        self.trailing_frames = max(1, settings.trailing_silence_ms // self.frame_ms)
        self.minimum_voice_frames = max(1, settings.minimum_voice_ms // self.frame_ms)
        self.maximum_frames = max(
            1, int(settings.maximum_utterance_seconds * 1000 / self.frame_ms)
        )
        self.minimum_rms = settings.vad_minimum_rms
        self.noise_ratio = settings.vad_noise_ratio
        self.noise_rms = max(1.0, self.minimum_rms / self.noise_ratio)
        self.pre_roll: deque[bytes] = deque(maxlen=self.pre_roll_frames)
        self.active_frames: list[bytes] | None = None
        self.start_run = 0
        self.trailing_run = 0
        self.voiced_frames = 0

    @property
    def threshold(self) -> float:
        return max(self.minimum_rms, self.noise_rms * self.noise_ratio)

    def feed(self, frame: bytes) -> bytes | None:
        rms = pcm16_rms(frame)
        voiced = rms >= self.threshold
        if self.active_frames is None:
            self.pre_roll.append(frame)
            if voiced:
                self.start_run += 1
            else:
                self.start_run = 0
                self.noise_rms = self.noise_rms * 0.95 + rms * 0.05
            if self.start_run >= self.start_frames:
                self.active_frames = list(self.pre_roll)
                self.voiced_frames = self.start_run
                self.trailing_run = 0
            return None

        self.active_frames.append(frame)
        if voiced:
            self.voiced_frames += 1
            self.trailing_run = 0
        else:
            self.trailing_run += 1
        if (
            self.trailing_run >= self.trailing_frames
            or len(self.active_frames) >= self.maximum_frames
        ):
            return self.finish()
        return None

    def finish(self) -> bytes | None:
        frames = self.active_frames or []
        enough_voice = self.voiced_frames >= self.minimum_voice_frames
        self.pre_roll.clear()
        self.active_frames = None
        self.start_run = 0
        self.trailing_run = 0
        self.voiced_frames = 0
        return b"".join(frames) if frames and enough_voice else None


def pcm16_to_wav(pcm: bytes) -> bytes:
    encoded = io.BytesIO()
    with wave.open(encoded, "wb") as destination:
        destination.setnchannels(CHANNELS)
        destination.setsampwidth(SAMPLE_WIDTH)
        destination.setframerate(SAMPLE_RATE)
        destination.writeframes(pcm)
    return encoded.getvalue()


class WakeWordRouter:
    """Accept a wake suffix or arm exactly one following utterance."""

    def __init__(self, armed_seconds: float) -> None:
        self.armed_seconds = armed_seconds
        self.armed_until = 0.0

    @staticmethod
    def wake_match(text: str) -> tuple[re.Match[str], int] | None:
        words = list(WORD_RE.finditer(text))
        for index, match in enumerate(words[:2]):
            if match.group(0).casefold() in WAKE_WORDS:
                return match, index
        return None

    def route(self, text: str, now: float | None = None) -> tuple[str | None, str]:
        now = time.monotonic() if now is None else now
        text = text.strip()
        if not text:
            return None, "ignored"

        was_armed = bool(self.armed_until and now <= self.armed_until)
        if self.armed_until and not was_armed:
            self.armed_until = 0.0
        hit = self.wake_match(text)

        if was_armed:
            self.armed_until = 0.0
            if hit:
                suffix = text[hit[0].end() :].strip(TRIM_AFTER_WAKE)
                if suffix:
                    return suffix, "command"
                self.armed_until = now + self.armed_seconds
                return None, "armed"
            return text, "command"

        if not hit:
            return None, "ignored"
        suffix = text[hit[0].end() :].strip(TRIM_AFTER_WAKE)
        if suffix:
            return suffix, "command"
        self.armed_until = now + self.armed_seconds
        return None, "armed"


def bounded_read(response: Any, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("HTTP response exceeded its size limit")
    return data


def post_json(url: str, payload: dict[str, Any], timeout: float, token: str = "") -> Any:
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "CerebrusVoiceBridge/1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=encoded, headers=headers, method="POST")
    with _LOCAL_HTTP_OPENER.open(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        if content_type != "application/json":
            raise RuntimeError("local API returned a non-JSON response")
        raw = bounded_read(response, 4 * 1024 * 1024)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("local API returned invalid JSON") from error


def transcribe_wav(settings: Settings, wav_bytes: bytes) -> str:
    request = urllib.request.Request(
        settings.asr_url,
        data=wav_bytes,
        headers={
            "Content-Type": "audio/wav",
            "Accept": "application/json",
            "User-Agent": "CerebrusVoiceBridge/1",
        },
        method="POST",
    )
    with _LOCAL_HTTP_OPENER.open(
        request, timeout=settings.asr_timeout_seconds
    ) as response:
        if response.headers.get_content_type() != "application/json":
            raise RuntimeError("ASR returned a non-JSON response")
        raw = bounded_read(response, 1024 * 1024)
    try:
        payload = json.loads(raw)
        text = payload["text"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise RuntimeError("ASR returned an invalid response") from error
    if not isinstance(text, str) or len(text) > 64_000:
        raise RuntimeError("ASR transcription has an invalid type or length")
    return text.strip()


def extract_final_text(payload: Any) -> str:
    try:
        choice = payload["choices"][0]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("OpenClaw response has no final message") from error
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                item_text = item.get("text")
                if isinstance(item_text, str):
                    parts.append(item_text)
        text = "\n".join(parts)
    else:
        raise RuntimeError("OpenClaw final message has unsupported content")
    text = text.strip()
    if not text or len(text) > 1_000_000:
        raise RuntimeError("OpenClaw final message is empty or too large")
    return text


def ask_openclaw(settings: Settings, command: str) -> str:
    payload = post_json(
        settings.openclaw_url,
        {
            "model": settings.openclaw_model,
            "messages": [{"role": "user", "content": command}],
            "stream": False,
            "user": settings.openclaw_user,
        },
        settings.openclaw_timeout_seconds,
        settings.openclaw_token,
    )
    return extract_final_text(payload)


def chunk_for_tts(text: str, maximum: int = 140) -> list[str]:
    """Break prose on natural boundaries without exceeding Audio8's comfort zone."""
    if not 20 <= maximum <= 300:
        raise ValueError("maximum chunk length must be between 20 and 300")
    remaining = re.sub(r"\s+", " ", text).strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= maximum:
            chunks.append(remaining)
            break
        window = remaining[: maximum + 1]
        split_at = -1
        for match in re.finditer(r"[.!?;:]\s+", window):
            split_at = match.end() - 1
        if split_at < maximum // 3:
            split_at = window.rfind(" ", maximum // 3, maximum + 1)
        if split_at < 1:
            split_at = maximum
        chunk = remaining[:split_at].strip()
        if not chunk:
            chunk = remaining[:maximum]
            split_at = maximum
        chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    return chunks


def bounded_spoken_chunks(text: str) -> tuple[list[str], bool]:
    """Apply immutable speech caps before asking Audio8 to synthesize anything."""
    normalized = re.sub(r"\s+", " ", text).strip()
    character_limited = normalized[:MAX_SPOKEN_CHARACTERS].rstrip()
    all_chunks = chunk_for_tts(character_limited, TTS_CHUNK_CHARACTERS)
    chunks = all_chunks[:MAX_TTS_CHUNKS]
    truncated = (
        len(normalized) > len(character_limited)
        or len(all_chunks) > len(chunks)
    )
    return chunks, truncated


def validate_tts_wav(payload: bytes) -> float:
    if len(payload) < 44 or len(payload) > 32 * 1024 * 1024:
        raise RuntimeError("Audio8 returned an invalid WAV size")
    if payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise RuntimeError("Audio8 returned a non-WAV response")
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getcomptype() != "NONE" or source.getsampwidth() != 2:
                raise RuntimeError("Audio8 WAV must be uncompressed PCM16")
            if source.getnchannels() not in {1, 2}:
                raise RuntimeError("Audio8 WAV has an unsupported channel count")
            rate = source.getframerate()
            frames = source.getnframes()
            if not 8_000 <= rate <= 192_000 or not frames:
                raise RuntimeError("Audio8 WAV has invalid timing metadata")
            duration = frames / rate
            if duration > 180:
                raise RuntimeError("Audio8 WAV is unexpectedly long")
            expected = frames * source.getnchannels() * source.getsampwidth()
            if len(source.readframes(frames)) != expected:
                raise RuntimeError("Audio8 WAV is truncated")
    except (EOFError, wave.Error) as error:
        raise RuntimeError("Audio8 returned a malformed WAV") from error
    return duration


def synthesize(settings: Settings, text: str) -> bytes:
    encoded = json.dumps(
        {
            "model": settings.tts_model,
            "input": text,
            "response_format": "wav",
            "max_new_tokens": 1024,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        settings.tts_url,
        data=encoded,
        headers={
            "Content-Type": "application/json",
            "Accept": "audio/wav",
            "User-Agent": "CerebrusVoiceBridge/1",
        },
        method="POST",
    )
    with _LOCAL_HTTP_OPENER.open(
        request, timeout=settings.tts_timeout_seconds
    ) as response:
        if response.headers.get_content_type() not in {"audio/wav", "audio/x-wav"}:
            raise RuntimeError("Audio8 returned an unexpected content type")
        wav_bytes = bounded_read(response, 32 * 1024 * 1024)
    validate_tts_wav(wav_bytes)
    return wav_bytes


class Recorder:
    def __init__(self, settings: Settings) -> None:
        self.frame_bytes = SAMPLE_RATE * SAMPLE_WIDTH * settings.frame_ms // 1000
        self.process = subprocess.Popen(
            [
                "/usr/bin/arecord",
                "--quiet",
                "--device",
                settings.capture_device,
                "--file-type",
                "raw",
                "--format",
                "S16_LE",
                "--channels",
                str(CHANNELS),
                "--rate",
                str(SAMPLE_RATE),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def read_frame(self) -> bytes:
        assert self.process.stdout is not None
        chunks = bytearray()
        while len(chunks) < self.frame_bytes:
            piece = self.process.stdout.read(self.frame_bytes - len(chunks))
            if not piece:
                raise RuntimeError("CP900 capture ended")
            chunks.extend(piece)
        return bytes(chunks)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.process.stdout is not None:
            self.process.stdout.close()


def play_wav(settings: Settings, wav_bytes: bytes) -> None:
    duration = validate_tts_wav(wav_bytes)
    process = subprocess.Popen(
        [
            "/usr/bin/aplay",
            "--quiet",
            "--device",
            settings.playback_device,
            "--file-type",
            "wav",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        process.communicate(wav_bytes, timeout=max(20, duration + 20))
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise RuntimeError("CP900 playback timed out")
    if process.returncode:
        raise RuntimeError(f"CP900 playback failed with status {process.returncode}")


class VoiceBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.router = WakeWordRouter(settings.armed_seconds)
        self.stop_event = threading.Event()
        self.recorder: Recorder | None = None

    def log_text(self, label: str, text: str) -> None:
        if self.settings.log_transcripts:
            print(f"{label}: {text}", flush=True)
        else:
            print(f"{label}: {len(text)} characters (content logging disabled)", flush=True)

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()
        if self.recorder is not None:
            self.recorder.stop()

    def handle_utterance(self, pcm: bytes) -> bool:
        transcript = transcribe_wav(self.settings, pcm16_to_wav(pcm))
        if not transcript:
            print("ASR returned no speech", flush=True)
            return False
        self.log_text("ASR utterance", transcript)
        command, state = self.router.route(transcript)
        if state == "ignored":
            print("Wake word absent; utterance ignored", flush=True)
            return False
        if state == "armed":
            print("Wake word heard; listening for one request", flush=True)
            return False
        assert command is not None
        self.log_text("Voice request accepted", command)
        answer = ask_openclaw(self.settings, command)
        chunks, truncated = bounded_spoken_chunks(answer)
        if truncated:
            print(
                "OpenClaw response truncated for speech at "
                f"{MAX_SPOKEN_CHARACTERS} characters/{MAX_TTS_CHUNKS} chunks",
                flush=True,
            )
        print(
            f"OpenClaw final response: {len(answer)} characters in "
            f"{len(chunks)} chunks",
            flush=True,
        )
        for chunk in chunks:
            if self.stop_event.is_set():
                break
            play_wav(self.settings, synthesize(self.settings, chunk))
        return bool(chunks)

    def capture_one(self) -> bool:
        vad = EnergyVad(self.settings)
        self.recorder = Recorder(self.settings)
        try:
            while not self.stop_event.is_set():
                utterance = vad.feed(self.recorder.read_frame())
                if utterance is not None:
                    self.recorder.stop()
                    self.recorder = None
                    return self.handle_utterance(utterance)
        finally:
            if self.recorder is not None:
                self.recorder.stop()
                self.recorder = None
        return False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        print(
            "Voice bridge ready; raw microphone audio and transcripts are not persisted",
            flush=True,
        )
        while not self.stop_event.is_set():
            try:
                played = self.capture_one()
                if played and self.stop_event.wait(
                    self.settings.playback_cooldown_seconds
                ):
                    break
            except urllib.error.HTTPError as error:
                print(f"Local voice API returned HTTP {error.code}; retrying", flush=True)
                self.stop_event.wait(2)
            except urllib.error.URLError:
                print("Local voice API is unavailable; retrying", flush=True)
                self.stop_event.wait(2)
            except Exception as error:
                if not self.stop_event.is_set():
                    print(f"Voice bridge error: {type(error).__name__}; retrying", flush=True)
                    self.stop_event.wait(2)


def main() -> None:
    VoiceBridge(Settings.from_environment()).run()


if __name__ == "__main__":
    main()
