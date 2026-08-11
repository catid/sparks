#!/usr/bin/env python3
"""RAM-only CP900 wake-word bridge from Qwen ASR to OpenClaw and Audio8."""

from __future__ import annotations

import io
import ipaddress
import json
import math
import multiprocessing
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
from datetime import datetime, timezone
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any


SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
WORD_RE = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
# Keep the historical ASR misspelling as an input-only tolerance alias. It is
# never surfaced in prompts, status, logs, or user-facing identity.
WAKE_WORDS = frozenset({"cerberus", "cerebrus"})
TRIM_AFTER_WAKE = " \t\r\n,.:;!?—–-"
MAX_SPOKEN_CHARACTERS = 2_000
MAX_TTS_CHUNKS = 16
TTS_CHUNK_CHARACTERS = 140
THINKING_CUE_TEXT = "Mm."
THINKING_CUE_MAX_SECONDS = 0.65
THINKING_CUE_TARGET_PEAK = 0.07
THINKING_CUE_FADE_SECONDS = 0.035
STATUS_FILENAME = "status.json"
STATUS_MAX_BYTES = 16 * 1024
STATUS_HEARTBEAT_SECONDS = 2.0


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
    state_dir: str | None

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
        user = os.environ.get("VOICE_OPENCLAW_USER", "cerberus3-voice").strip()
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
            state_dir=os.environ.get("VOICE_STATE_DIR", "").strip() or None,
        )


@dataclass(frozen=True)
class TtsClientSettings:
    """The only configuration allowed across the synthesis process boundary."""

    tts_url: str
    tts_model: str
    tts_timeout_seconds: float

    @classmethod
    def from_settings(
        cls,
        settings: Settings | "TtsClientSettings",
    ) -> "TtsClientSettings":
        return cls(
            tts_url=settings.tts_url,
            tts_model=settings.tts_model,
            tts_timeout_seconds=settings.tts_timeout_seconds,
        )


def utc_timestamp(epoch: float | None = None) -> str:
    """Return a compact, unambiguous UTC timestamp for the status API."""
    when = time.time() if epoch is None else epoch
    return (
        datetime.fromtimestamp(when, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class StatusPublisher:
    """Publish a bounded, content-free snapshot for the local dashboard.

    The public document has a deliberately closed schema. Callers can only set
    enumerated pipeline states, timestamps, durations, and numeric chunk
    progress; microphone text and model output have no field through which to
    enter the document.
    """

    OVERALL_STATES = frozenset(
        {"starting", "ready", "busy", "armed", "degraded", "stopping", "stopped"}
    )
    OVERALL_STAGES = frozenset(
        {
            "starting",
            "listening",
            "speech_detected",
            "asr",
            "watchword",
            "openclaw",
            "tts_synthesis",
            "tts_playback",
            "cooldown",
            "retry_wait",
            "stopping",
            "stopped",
        }
    )
    WAKE_STATES = frozenset(
        {"listening", "checking", "armed", "triggered", "not_detected", "stopped"}
    )
    COMPONENT_STATES = {
        "asr": frozenset({"idle", "processing", "ok", "error"}),
        "openclaw": frozenset({"idle", "thinking", "ok", "error"}),
        "tts": frozenset(
            {"idle", "synthesizing", "playing", "cooldown", "ok", "error"}
        ),
    }
    ERROR_STAGES = frozenset(
        {"capture", "asr", "watchword", "openclaw", "tts_synthesis", "tts_playback"}
    )
    PIPELINE_MODES = frozenset(
        {
            "idle",
            "scanning",
            "armed",
            "request",
            "responding",
            "complete",
            "error",
            "stopped",
        }
    )
    PIPELINE_STEPS = ("heard_name", "asr", "openclaw", "tts", "play")
    PIPELINE_STEP_STATES = frozenset({"idle", "active", "complete", "error"})

    def __init__(
        self,
        state_dir: str | None,
        heartbeat_seconds: float = STATUS_HEARTBEAT_SECONDS,
    ) -> None:
        if not 0.25 <= heartbeat_seconds <= 30:
            raise ValueError("status heartbeat must be between 0.25 and 30 seconds")
        self.path: Path | None = None
        if state_dir:
            candidate = Path(state_dir)
            if not candidate.is_absolute():
                raise RuntimeError("VOICE_STATE_DIR must be an absolute path")
            self.path = candidate / STATUS_FILENAME
        self.heartbeat_seconds = heartbeat_seconds
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False
        self._sequence = 0
        self._temporary_sequence = 0
        self._component_started: dict[str, float] = {}
        self._tts_started_monotonic: float | None = None
        self._last_write_error_type: str | None = None
        now_epoch = time.time()
        now_text = utc_timestamp(now_epoch)
        self._document: dict[str, Any] = {
            "schema": 1,
            "service": "cerberus-voice",
            "device": "Cerberus",
            "pid": os.getpid(),
            "instance_id": f"{os.getpid()}-{int(now_epoch * 1000)}",
            "sequence": 0,
            "started_at": now_text,
            "stopped_at": None,
            "updated_at": now_text,
            "updated_at_epoch": now_epoch,
            "heartbeat_at": now_text,
            "overall": {
                "state": "starting",
                "stage": "starting",
                "stage_started_at": now_text,
            },
            "wake_word": {
                "state": "listening",
                "last_trigger_at": None,
                "armed_until": None,
            },
            "asr": self._new_component("idle"),
            "openclaw": self._new_component("idle"),
            "tts": {
                **self._new_component("idle"),
                "chunk_index": 0,
                "chunk_total": 0,
                "synthesis_chunk_index": 0,
                "playback_chunk_index": 0,
                "chunk_started_at": None,
            },
            # Turn-scoped and deliberately content-free.  This prevents a
            # display from mistaking component successes retained from an old
            # request for progress on the current request.  Schema-1 readers
            # safely ignore this backwards-compatible addition.
            "pipeline": self._new_pipeline(),
            "last_error": None,
        }

    @staticmethod
    def _new_component(state: str) -> dict[str, Any]:
        return {
            "state": state,
            "started_at": None,
            "completed_at": None,
            "duration_seconds": None,
            "last_success_at": None,
        }

    @classmethod
    def _new_pipeline(cls) -> dict[str, Any]:
        return {
            "active": False,
            "mode": "idle",
            "steps": {step: "idle" for step in cls.PIPELINE_STEPS},
        }

    def _pipeline_reset_locked(
        self,
        *,
        preserve_heard_name: bool = False,
    ) -> None:
        pipeline = self._document["pipeline"]
        pipeline["active"] = True
        pipeline["mode"] = "scanning"
        pipeline["steps"] = {
            step: (
                "complete" if step == "heard_name" and preserve_heard_name else "idle"
            )
            for step in self.PIPELINE_STEPS
        }

    def _pipeline_idle_locked(self) -> None:
        pipeline = self._document["pipeline"]
        pipeline["active"] = False
        pipeline["mode"] = "idle"
        pipeline["steps"] = {step: "idle" for step in self.PIPELINE_STEPS}

    def _pipeline_step_locked(self, step: str, state: str) -> None:
        if step not in self.PIPELINE_STEPS or state not in self.PIPELINE_STEP_STATES:
            raise ValueError("invalid pipeline progress")
        self._document["pipeline"]["steps"][step] = state

    def _pipeline_mode_locked(self, mode: str, *, active: bool = True) -> None:
        if mode not in self.PIPELINE_MODES:
            raise ValueError("invalid pipeline mode")
        pipeline = self._document["pipeline"]
        pipeline["mode"] = mode
        pipeline["active"] = active

    def _pipeline_error_locked(self, stage: str) -> None:
        step = {
            # Capture has no separate display band.  It prevents ASR from
            # receiving audio, so surface it on ASR rather than showing an
            # unhelpful generic error with every step idle.
            "capture": "asr",
            "asr": "asr",
            "watchword": "heard_name",
            "openclaw": "openclaw",
            "tts_synthesis": "tts",
            "tts_playback": "play",
        }.get(stage)
        # A failed pipeline is terminal until retry.  In particular, chunked
        # TTS can otherwise leave TTS/PLAY marked active when the other one
        # fails between chunks.
        steps = self._document["pipeline"]["steps"]
        for active_step, state in steps.items():
            if state == "active":
                steps[active_step] = "complete"
        if step is not None:
            self._pipeline_step_locked(step, "error")
        self._pipeline_mode_locked("error", active=False)

    def _pipeline_retained_error_locked(self, *, armed: bool = False) -> bool:
        """Restore the visible failed band after an unrelated listening pass."""
        last_error = self._document.get("last_error")
        if not isinstance(last_error, dict):
            return False
        stage = last_error.get("stage")
        failed_step = {
            "capture": "asr",
            "asr": "asr",
            "watchword": "heard_name",
            "openclaw": "openclaw",
            "tts_synthesis": "tts",
            "tts_playback": "play",
        }.get(stage)
        if failed_step is None:
            return False

        completed_before = {
            "capture": (),
            "asr": (),
            "watchword": ("asr",),
            "openclaw": ("heard_name", "asr"),
            "tts_synthesis": ("heard_name", "asr", "openclaw"),
            "tts_playback": ("heard_name", "asr", "openclaw", "tts"),
        }[stage]
        steps = {step: "idle" for step in self.PIPELINE_STEPS}
        for completed_step in completed_before:
            steps[completed_step] = "complete"
        if armed:
            steps["heard_name"] = "complete"
        steps[failed_step] = "error"
        pipeline = self._document["pipeline"]
        pipeline["steps"] = steps
        pipeline["mode"] = "armed" if armed else "error"
        pipeline["active"] = armed
        return True

    def _pipeline_armed_locked(self) -> None:
        pipeline = self._document["pipeline"]
        pipeline["active"] = True
        pipeline["mode"] = "armed"
        pipeline["steps"] = {
            "heard_name": "complete",
            "asr": "idle",
            "openclaw": "idle",
            "tts": "idle",
            "play": "idle",
        }

    @staticmethod
    def _safe_error_type(error: BaseException) -> str:
        # Class names are useful operationally but error messages can contain a
        # request, response, URL query, or other private content.
        name = type(error).__name__[:80]
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "Exception"

    @staticmethod
    def _duration(started: float | None) -> float | None:
        if started is None:
            return None
        return round(max(0.0, time.monotonic() - started), 3)

    def start(self) -> None:
        if self.path is None:
            return
        with self._lock:
            if self._started:
                return
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._started = True
            self._publish_locked()
            self._thread = threading.Thread(
                target=self._heartbeat_loop,
                name="voice-status-heartbeat",
                daemon=True,
            )
            self._thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            with self._lock:
                wake = self._document["wake_word"]
                armed_until = wake["armed_until"]
                if (
                    wake["state"] == "armed"
                    and isinstance(armed_until, str)
                    and self._armed_timestamp_expired(armed_until)
                ):
                    wake["state"] = "listening"
                    wake["armed_until"] = None
                    if not self._pipeline_retained_error_locked():
                        self._pipeline_idle_locked()
                    if self._document["overall"]["stage"] == "listening":
                        self._set_overall_locked("ready", "listening")
                self._publish_locked()

    @staticmethod
    def _armed_timestamp_expired(timestamp: str) -> bool:
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return True
        return parsed.timestamp() <= time.time()

    def _publish_locked(self) -> None:
        if not self._started or self.path is None:
            return
        self._sequence += 1
        now = time.time()
        self._document["sequence"] = self._sequence
        self._document["updated_at"] = utc_timestamp(now)
        self._document["updated_at_epoch"] = now
        self._document["heartbeat_at"] = self._document["updated_at"]
        encoded = (
            json.dumps(self._document, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > STATUS_MAX_BYTES:
            raise RuntimeError("voice status document exceeded its fixed size limit")

        self._temporary_sequence += 1
        temporary = self.path.with_name(
            f".{STATUS_FILENAME}.{os.getpid()}.{self._temporary_sequence}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd: int | None = None
        try:
            fd = os.open(temporary, flags, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as destination:
                fd = None
                destination.write(encoded)
                destination.flush()
                os.fsync(destination.fileno())
            os.replace(temporary, self.path)
            self._last_write_error_type = None
        except OSError as error:
            error_type = self._safe_error_type(error)
            if error_type != self._last_write_error_type:
                print(f"Voice status publication failed: {error_type}", flush=True)
                self._last_write_error_type = error_type
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        finally:
            if fd is not None:
                os.close(fd)

    def _set_overall_locked(self, state: str, stage: str) -> None:
        if state not in self.OVERALL_STATES or stage not in self.OVERALL_STAGES:
            raise ValueError("invalid voice status transition")
        overall = self._document["overall"]
        if overall["stage"] != stage:
            overall["stage_started_at"] = utc_timestamp()
        overall["state"] = state
        overall["stage"] = stage

    def _clear_error_locked(self, *stages: str) -> None:
        """Clear a retained failure only after that same operation recovers."""
        last_error = self._document.get("last_error")
        if isinstance(last_error, dict) and last_error.get("stage") in stages:
            self._document["last_error"] = None

    def _transition(self, state: str, stage: str) -> None:
        with self._lock:
            self._set_overall_locked(state, stage)
            self._publish_locked()

    def ready(self) -> None:
        with self._lock:
            self._document["wake_word"]["state"] = "listening"
            self._document["wake_word"]["armed_until"] = None
            if not self._pipeline_retained_error_locked():
                self._pipeline_idle_locked()
            self._set_overall_locked("ready", "listening")
            self._publish_locked()

    def resume_listening(self) -> None:
        """Recover from a rejected VAD burst without discarding a live arm."""
        with self._lock:
            wake = self._document["wake_word"]
            armed_until = wake.get("armed_until")
            armed = (
                isinstance(armed_until, str)
                and not self._armed_timestamp_expired(armed_until)
                and wake.get("state") not in {"triggered", "stopped"}
            )
            if armed:
                wake["state"] = "armed"
                if not self._pipeline_retained_error_locked(armed=True):
                    self._pipeline_armed_locked()
                self._set_overall_locked("armed", "listening")
            else:
                wake["state"] = "listening"
                wake["armed_until"] = None
                if not self._pipeline_retained_error_locked():
                    self._pipeline_idle_locked()
                self._set_overall_locked("ready", "listening")
            self._publish_locked()

    def speech_detected(self) -> None:
        with self._lock:
            wake = self._document["wake_word"]
            armed_until = wake.get("armed_until")
            armed = (
                isinstance(armed_until, str)
                and not self._armed_timestamp_expired(armed_until)
                and wake.get("state") == "armed"
            )
            self._pipeline_reset_locked(
                preserve_heard_name=armed,
            )
            self._set_overall_locked("busy", "speech_detected")
            self._publish_locked()

    def begin_asr(self) -> None:
        with self._lock:
            # Reaching ASR proves capture recovered from any prior device/read
            # failure. Preserve unrelated failures until their own stage wins.
            self._clear_error_locked("capture")
            wake = self._document["wake_word"]
            armed_until = wake.get("armed_until")
            armed = (
                isinstance(armed_until, str)
                and not self._armed_timestamp_expired(armed_until)
            )
            pipeline = self._document["pipeline"]
            if not pipeline["active"]:
                self._pipeline_reset_locked(
                    preserve_heard_name=armed,
                )
            self._pipeline_step_locked("asr", "active")
            self._pipeline_mode_locked("scanning")
        self._begin_component("asr", "processing", "asr")
        with self._lock:
            self._document["wake_word"]["state"] = "checking"
            self._publish_locked()

    def begin_openclaw(self) -> None:
        with self._lock:
            self._pipeline_step_locked("heard_name", "complete")
            self._pipeline_step_locked("asr", "complete")
            self._pipeline_step_locked("openclaw", "active")
            self._pipeline_mode_locked("request")
        self._begin_component("openclaw", "thinking", "openclaw")

    def begin_tts(self, chunk_total: int) -> None:
        if not 0 <= chunk_total <= MAX_TTS_CHUNKS:
            raise ValueError("invalid TTS chunk count")
        with self._lock:
            now = utc_timestamp()
            self._tts_started_monotonic = time.monotonic()
            tts = self._document["tts"]
            tts.update(
                {
                    "state": "synthesizing" if chunk_total else "ok",
                    "started_at": now,
                    "completed_at": None,
                    "duration_seconds": None,
                    "chunk_index": 1 if chunk_total else 0,
                    "chunk_total": chunk_total,
                    "synthesis_chunk_index": 1 if chunk_total else 0,
                    "playback_chunk_index": 0,
                    "chunk_started_at": now if chunk_total else None,
                }
            )
            self._pipeline_step_locked("openclaw", "complete")
            self._pipeline_step_locked("tts", "active" if chunk_total else "complete")
            self._pipeline_step_locked("play", "idle" if chunk_total else "complete")
            self._pipeline_mode_locked(
                "responding" if chunk_total else "complete",
                active=True,
            )
            self._set_overall_locked(
                "busy" if chunk_total else "ready",
                "tts_synthesis" if chunk_total else "listening",
            )
            self._publish_locked()

    def tts_phase(self, state: str, chunk_index: int) -> None:
        if state not in {"synthesizing", "playing"}:
            raise ValueError("invalid TTS phase")
        with self._lock:
            tts = self._document["tts"]
            total = tts["chunk_total"]
            if not isinstance(total, int) or not 1 <= chunk_index <= total:
                raise ValueError("invalid TTS chunk progress")
            tts["state"] = state
            tts["chunk_index"] = chunk_index
            tts[
                "synthesis_chunk_index"
                if state == "synthesizing"
                else "playback_chunk_index"
            ] = chunk_index
            tts["chunk_started_at"] = utc_timestamp()
            if state == "synthesizing":
                self._pipeline_step_locked("tts", "active")
                # Once playback has started, keep that progress active while
                # later chunks synthesize so the display never moves backward.
                if chunk_index > 1:
                    self._pipeline_step_locked("play", "active")
            else:
                self._pipeline_step_locked(
                    "tts", "complete" if chunk_index == total else "active"
                )
                self._pipeline_step_locked("play", "active")
            self._pipeline_mode_locked("responding")
            self._set_overall_locked(
                "busy", "tts_synthesis" if state == "synthesizing" else "tts_playback"
            )
            self._publish_locked()

    def begin_cooldown(self) -> None:
        with self._lock:
            self._document["tts"]["state"] = "cooldown"
            self._document["tts"]["chunk_started_at"] = utc_timestamp()
            self._pipeline_step_locked("tts", "complete")
            self._pipeline_step_locked("play", "complete")
            self._pipeline_mode_locked("complete")
            self._set_overall_locked("busy", "cooldown")
            self._publish_locked()

    def finish_tts(self) -> None:
        with self._lock:
            now = utc_timestamp()
            tts = self._document["tts"]
            tts["state"] = "ok"
            tts["completed_at"] = now
            tts["duration_seconds"] = self._duration(self._tts_started_monotonic)
            tts["last_success_at"] = now
            tts["chunk_started_at"] = None
            self._tts_started_monotonic = None
            if tts["chunk_total"]:
                self._clear_error_locked("tts_synthesis", "tts_playback")
            self._pipeline_step_locked("tts", "complete")
            self._pipeline_step_locked("play", "complete")
            self._pipeline_mode_locked("complete")
            self._publish_locked()

    def wake_not_detected(self) -> None:
        with self._lock:
            self._document["wake_word"].update(
                {"state": "not_detected", "armed_until": None}
            )
            self._clear_error_locked("watchword")
            if not self._pipeline_retained_error_locked():
                self._pipeline_idle_locked()
            self._set_overall_locked("ready", "listening")
            self._publish_locked()

    def wake_armed(self, seconds: float) -> None:
        with self._lock:
            now = time.time()
            wake = self._document["wake_word"]
            wake.update(
                {
                    "state": "armed",
                    "last_trigger_at": utc_timestamp(now),
                    "armed_until": utc_timestamp(now + seconds),
                }
            )
            self._clear_error_locked("watchword")
            if not self._pipeline_retained_error_locked(armed=True):
                self._pipeline_armed_locked()
            self._set_overall_locked("armed", "listening")
            self._publish_locked()

    def wake_triggered(self, heard_now: bool) -> None:
        with self._lock:
            wake = self._document["wake_word"]
            wake["state"] = "triggered"
            wake["armed_until"] = None
            if heard_now or wake["last_trigger_at"] is None:
                wake["last_trigger_at"] = utc_timestamp()
            self._clear_error_locked("watchword")
            self._pipeline_step_locked("heard_name", "complete")
            self._pipeline_step_locked("asr", "complete")
            self._pipeline_mode_locked("request")
            self._set_overall_locked("busy", "watchword")
            self._publish_locked()

    def _begin_component(self, component: str, state: str, stage: str) -> None:
        if state not in self.COMPONENT_STATES[component]:
            raise ValueError("invalid component state")
        with self._lock:
            now = utc_timestamp()
            self._component_started[component] = time.monotonic()
            details = self._document[component]
            details.update(
                {
                    "state": state,
                    "started_at": now,
                    "completed_at": None,
                    "duration_seconds": None,
                }
            )
            self._set_overall_locked("busy", stage)
            self._publish_locked()

    def component_ok(self, component: str) -> None:
        if component not in {"asr", "openclaw"}:
            raise ValueError("invalid status component")
        with self._lock:
            now = utc_timestamp()
            details = self._document[component]
            details["state"] = "ok"
            details["completed_at"] = now
            details["duration_seconds"] = self._duration(
                self._component_started.pop(component, None)
            )
            details["last_success_at"] = now
            self._clear_error_locked(component)
            self._pipeline_step_locked(component, "complete")
            self._publish_locked()

    def fail(self, stage: str, error: BaseException) -> None:
        if stage not in self.ERROR_STAGES:
            raise ValueError("invalid failure stage")
        with self._lock:
            now = utc_timestamp()
            component = (
                "asr"
                if stage == "asr"
                else "openclaw"
                if stage == "openclaw"
                else "tts"
                if stage.startswith("tts_")
                else None
            )
            if component:
                details = self._document[component]
                details["state"] = "error"
                details["completed_at"] = now
                started = (
                    self._tts_started_monotonic
                    if component == "tts"
                    else self._component_started.pop(component, None)
                )
                details["duration_seconds"] = self._duration(started)
            self._document["last_error"] = {
                "stage": stage,
                "type": self._safe_error_type(error),
                "at": now,
            }
            self._pipeline_error_locked(stage)
            self._set_overall_locked("degraded", "retry_wait")
            self._publish_locked()

    def stop(self) -> None:
        if self.path is None:
            return
        with self._lock:
            if not self._started:
                return
            self._set_overall_locked("stopping", "stopping")
            self._publish_locked()
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=max(1.0, self.heartbeat_seconds * 2))
        with self._lock:
            now = utc_timestamp()
            self._document["wake_word"]["state"] = "stopped"
            self._document["stopped_at"] = now
            self._pipeline_mode_locked("stopped", active=False)
            self._set_overall_locked("stopped", "stopped")
            self._publish_locked()


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
        "User-Agent": "CerberusVoiceBridge/1",
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
            "User-Agent": "CerberusVoiceBridge/1",
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


def synthesize(
    settings: Settings | TtsClientSettings,
    text: str,
    *,
    timeout_seconds: float | None = None,
) -> bytes:
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
            "User-Agent": "CerberusVoiceBridge/1",
        },
        method="POST",
    )
    with _LOCAL_HTTP_OPENER.open(
        request,
        timeout=(
            settings.tts_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        ),
    ) as response:
        if response.headers.get_content_type() not in {"audio/wav", "audio/x-wav"}:
            raise RuntimeError("Audio8 returned an unexpected content type")
        wav_bytes = bounded_read(response, 32 * 1024 * 1024)
    validate_tts_wav(wav_bytes)
    return wav_bytes


def soften_thinking_cue(payload: bytes) -> bytes:
    """Bound, attenuate, and fade the cached acknowledgement entirely in RAM."""
    validate_tts_wav(payload)
    with wave.open(io.BytesIO(payload), "rb") as source:
        channels = source.getnchannels()
        rate = source.getframerate()
        frame_count = min(
            source.getnframes(),
            max(1, int(rate * THINKING_CUE_MAX_SECONDS)),
        )
        frames = source.readframes(frame_count)

    sample_count = len(frames) // SAMPLE_WIDTH
    samples = list(struct.unpack(f"<{sample_count}h", frames))
    peak = max((abs(sample) for sample in samples), default=0)
    target = int(32_767 * THINKING_CUE_TARGET_PEAK)
    # Never amplify the conditioning voice. The additional cap keeps the cue
    # unobtrusive even when Audio8 already produced a quiet sample.
    gain = min(0.35, target / peak) if peak else 0.0
    fade_frames = min(
        max(1, int(rate * THINKING_CUE_FADE_SECONDS)),
        max(1, frame_count // 3),
    )
    softened: list[int] = []
    for sample_index, sample in enumerate(samples):
        frame_index = sample_index // channels
        envelope = 1.0
        if frame_index < fade_frames:
            envelope = frame_index / fade_frames
        elif frame_index >= frame_count - fade_frames:
            envelope = max(0.0, (frame_count - 1 - frame_index) / fade_frames)
        softened.append(round(sample * gain * envelope))

    destination = io.BytesIO()
    with wave.open(destination, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(rate)
        output.writeframes(struct.pack(f"<{len(softened)}h", *softened))
    cue = destination.getvalue()
    validate_tts_wav(cue)
    return cue


def fallback_thinking_cue() -> bytes:
    """Return a quiet, speech-free nasal hum until Audio8's cue is warm."""
    rate = SAMPLE_RATE
    frame_count = int(rate * 0.48)
    fade_frames = int(rate * THINKING_CUE_FADE_SECONDS)
    samples: list[int] = []
    for frame_index in range(frame_count):
        elapsed = frame_index / rate
        envelope = min(
            1.0,
            frame_index / max(1, fade_frames),
            (frame_count - 1 - frame_index) / max(1, fade_frames),
        )
        # A low fundamental plus two soft harmonics reads as an affirmative hum
        # without storing or imitating anyone's recorded voice.
        value = (
            math.sin(2 * math.pi * 132 * elapsed)
            + 0.33 * math.sin(2 * math.pi * 264 * elapsed)
            + 0.12 * math.sin(2 * math.pi * 396 * elapsed)
        )
        samples.append(round(32_767 * 0.035 * envelope * value / 1.45))
    destination = io.BytesIO()
    with wave.open(destination, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(SAMPLE_WIDTH)
        output.setframerate(rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return destination.getvalue()


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


class PlaybackProcess:
    """A bounded aplay child reading a WAV from an anonymous RAM-only file."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        timeout_seconds: float,
    ) -> None:
        self.process = process
        self.deadline = time.monotonic() + timeout_seconds

    def poll(self) -> bool:
        return_code = self.process.poll()
        if return_code is not None:
            if return_code:
                raise RuntimeError(
                    f"CP900 playback failed with status {return_code}"
                )
            return True
        if time.monotonic() >= self.deadline:
            self.cancel()
            raise RuntimeError("CP900 playback timed out")
        return False

    def cancel(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def wait(self, stop_event: threading.Event) -> bool:
        """Return False only for deliberate bridge shutdown."""
        while True:
            if stop_event.is_set():
                self.cancel()
                return False
            if self.poll():
                return True
            remaining = self.deadline - time.monotonic()
            stop_event.wait(min(0.05, remaining))


def start_playback(
    settings: Settings,
    wav_bytes: bytes,
    *,
    timeout_seconds: float | None = None,
) -> PlaybackProcess:
    """Launch playback without blocking synthesis or writing audio to disk."""
    duration = validate_tts_wav(wav_bytes)
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("anonymous RAM playback is unavailable")
    flags = getattr(os, "MFD_CLOEXEC", 0)
    memory_fd = os.memfd_create("cerberus-voice-wav", flags)
    try:
        view = memoryview(wav_bytes)
        while view:
            written = os.write(memory_fd, view)
            if written <= 0:
                raise RuntimeError("could not stage in-memory playback")
            view = view[written:]
        os.lseek(memory_fd, 0, os.SEEK_SET)
        process = subprocess.Popen(
            [
                "/usr/bin/aplay",
                "--quiet",
                "--device",
                settings.playback_device,
                "--file-type",
                "wav",
            ],
            stdin=memory_fd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    finally:
        os.close(memory_fd)
    return PlaybackProcess(
        process,
        max(20, duration + 20) if timeout_seconds is None else timeout_seconds,
    )


class SynthesisWorkerError(RuntimeError):
    """A content-free error reported by the isolated Audio8 client process."""


def _synthesis_worker_main(
    settings: TtsClientSettings,
    connection: Connection,
) -> None:
    """Serve one bounded request at a time without ever persisting its audio."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # multiprocessing inherits the service environment at exec. This worker
    # needs no environment configuration, so discard it before receiving model
    # text; no inherited credentials remain available during synthesis.
    os.environ.clear()
    try:
        while True:
            try:
                text = connection.recv()
            except EOFError:
                return
            if text is None:
                return
            try:
                wav_bytes = synthesize(settings, text)
            except Exception as error:
                connection.send(
                    ("error", StatusPublisher._safe_error_type(error))
                )
            else:
                connection.send(("ok", wav_bytes))
    finally:
        connection.close()


class SynthesisWorker:
    """One persistent, cancellable Audio8 client process with one job slot."""

    def __init__(self, settings: Settings | TtsClientSettings) -> None:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self.connection = parent
        self.process = context.Process(
            target=_synthesis_worker_main,
            args=(TtsClientSettings.from_settings(settings), child),
            name="audio8-client",
            daemon=True,
        )
        try:
            self.process.start()
        except Exception:
            child.close()
            parent.close()
            raise
        child.close()
        self.timeout_seconds = settings.tts_timeout_seconds
        self.deadline: float | None = None
        self.busy = False
        self.closed = False

    def submit(self, text: str) -> None:
        if self.closed or not self.process.is_alive():
            raise SynthesisWorkerError("Audio8 client process is unavailable")
        if self.busy:
            raise SynthesisWorkerError("Audio8 client already has a request")
        if not isinstance(text, str) or not 1 <= len(text) <= TTS_CHUNK_CHARACTERS:
            raise SynthesisWorkerError("Audio8 client request is invalid")
        try:
            self.connection.send(text)
        except (BrokenPipeError, EOFError, OSError) as error:
            self.cancel()
            raise SynthesisWorkerError(
                "Audio8 client connection failed"
            ) from error
        self.deadline = time.monotonic() + self.timeout_seconds
        self.busy = True

    def poll(self) -> bytes | None:
        if not self.busy:
            raise SynthesisWorkerError("Audio8 client has no request")
        try:
            result_ready = self.connection.poll(0)
        except (EOFError, OSError) as error:
            self.cancel()
            raise SynthesisWorkerError(
                "Audio8 client connection failed"
            ) from error
        if result_ready:
            try:
                result = self.connection.recv()
            except (EOFError, OSError) as error:
                self.cancel()
                raise SynthesisWorkerError(
                    "Audio8 client exited without a result"
                ) from error
            self.busy = False
            self.deadline = None
            if (
                not isinstance(result, tuple)
                or len(result) != 2
                or result[0] not in {"ok", "error"}
            ):
                self.cancel()
                raise SynthesisWorkerError("Audio8 client returned an invalid result")
            if result[0] == "error":
                remote_type = str(result[1])[:80]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", remote_type):
                    remote_type = "Exception"
                raise SynthesisWorkerError(
                    f"Audio8 synthesis failed ({remote_type})"
                )
            wav_bytes = result[1]
            if not isinstance(wav_bytes, bytes):
                self.cancel()
                raise SynthesisWorkerError("Audio8 client returned invalid audio")
            try:
                validate_tts_wav(wav_bytes)
            except Exception as error:
                self.cancel()
                raise SynthesisWorkerError(
                    "Audio8 client returned invalid audio"
                ) from error
            return wav_bytes
        if not self.process.is_alive():
            self.cancel()
            raise SynthesisWorkerError("Audio8 client exited during synthesis")
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.cancel()
            raise SynthesisWorkerError("Audio8 synthesis timed out")
        return None

    def cancel(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)
        self.connection.close()
        self.busy = False
        self.deadline = None

    def close(self) -> None:
        if self.closed:
            return
        if self.busy:
            self.cancel()
            return
        try:
            self.connection.send(None)
        except (BrokenPipeError, EOFError, OSError):
            pass
        self.process.join(timeout=1)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join(timeout=2)
        self.connection.close()
        self.closed = True


class VoiceBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.router = WakeWordRouter(settings.armed_seconds)
        self.stop_event = threading.Event()
        self.recorder: Recorder | None = None
        self.status = StatusPublisher(settings.state_dir)
        self._active_playback: PlaybackProcess | None = None
        self._playback_lock = threading.RLock()
        self._synthesis_worker: SynthesisWorker | None = None
        self._synthesis_worker_lock = threading.RLock()
        self._thinking_cue_lock = threading.Lock()
        self._thinking_cue = fallback_thinking_cue()

    def log_text(self, label: str, text: str) -> None:
        if self.settings.log_transcripts:
            print(f"{label}: {text}", flush=True)
        else:
            print(f"{label}: {len(text)} characters (content logging disabled)", flush=True)

    def request_stop(self, *_args: Any) -> None:
        self.stop_event.set()
        if self.recorder is not None:
            self.recorder.stop()
        self._cancel_playback()
        self._cancel_synthesis_worker()

    def _get_synthesis_worker(self) -> SynthesisWorker:
        with self._synthesis_worker_lock:
            worker = self._synthesis_worker
            if worker is None or worker.closed:
                if self.stop_event.is_set():
                    raise RuntimeError("voice bridge is stopping")
                worker = SynthesisWorker(self.settings)
                if self.stop_event.is_set():
                    worker.cancel()
                    raise RuntimeError("voice bridge is stopping")
                self._synthesis_worker = worker
            return worker

    def _cancel_synthesis_worker(self) -> None:
        with self._synthesis_worker_lock:
            worker = self._synthesis_worker
            self._synthesis_worker = None
            if worker is None:
                return
            try:
                worker.cancel()
            except Exception as error:
                print(
                    "Audio8 client cancellation failed: "
                    f"{StatusPublisher._safe_error_type(error)}",
                    flush=True,
                )

    def _close_synthesis_worker(self) -> None:
        with self._synthesis_worker_lock:
            worker = self._synthesis_worker
            self._synthesis_worker = None
            if worker is None:
                return
            try:
                worker.close()
            except Exception as error:
                print(
                    "Audio8 client shutdown failed: "
                    f"{StatusPublisher._safe_error_type(error)}",
                    flush=True,
                )

    def _warm_thinking_cue(self) -> None:
        """Try once at startup; failure leaves the always-ready safe hum."""
        try:
            synthesized = synthesize(
                self.settings,
                THINKING_CUE_TEXT,
                timeout_seconds=min(self.settings.tts_timeout_seconds, 5),
            )
            cue = soften_thinking_cue(synthesized)
        except Exception as error:
            print(
                "Thinking cue warmup failed: "
                f"{StatusPublisher._safe_error_type(error)}; using local hum",
                flush=True,
            )
            return
        if self.stop_event.is_set():
            return
        with self._thinking_cue_lock:
            self._thinking_cue = cue

    def _start_playback(
        self,
        wav_bytes: bytes,
        *,
        timeout_seconds: float | None = None,
    ) -> PlaybackProcess:
        with self._playback_lock:
            if self._active_playback is not None:
                raise RuntimeError("audio playback is already active")
            if self.stop_event.is_set():
                raise RuntimeError("voice bridge is stopping")
            playback = start_playback(
                self.settings,
                wav_bytes,
                timeout_seconds=timeout_seconds,
            )
            # A signal can arrive while Popen is creating the child. Recheck
            # under the re-entrant lock so that child can never escape stop.
            if self.stop_event.is_set():
                playback.cancel()
                raise RuntimeError("voice bridge is stopping")
            self._active_playback = playback
            return playback

    def _wait_playback(self, playback: PlaybackProcess) -> bool:
        try:
            return playback.wait(self.stop_event)
        finally:
            self._release_playback(playback)

    def _release_playback(self, playback: PlaybackProcess) -> None:
        with self._playback_lock:
            if self._active_playback is playback:
                self._active_playback = None

    def _cancel_playback(self, playback: PlaybackProcess | None = None) -> None:
        with self._playback_lock:
            active = self._active_playback
            if active is None or (playback is not None and active is not playback):
                return
            try:
                active.cancel()
            except Exception as error:
                print(
                    "Playback cancellation failed: "
                    f"{StatusPublisher._safe_error_type(error)}",
                    flush=True,
                )
            finally:
                if self._active_playback is active:
                    self._active_playback = None

    def _start_thinking_cue(self) -> PlaybackProcess | None:
        """Start the non-semantic cue without changing CLAW/TTS status."""
        with self._thinking_cue_lock:
            cue = self._thinking_cue
        try:
            return self._start_playback(cue, timeout_seconds=2.0)
        except Exception as error:
            print(
                "Thinking cue playback failed: "
                f"{StatusPublisher._safe_error_type(error)}; continuing",
                flush=True,
            )
            return None

    def _finish_thinking_cue(self, playback: PlaybackProcess | None) -> None:
        if playback is None:
            return
        try:
            self._wait_playback(playback)
        except Exception as error:
            # This courtesy sound is not part of the response pipeline. It must
            # never fail or falsely mark an otherwise healthy OpenClaw request.
            print(
                "Thinking cue playback failed: "
                f"{StatusPublisher._safe_error_type(error)}; continuing",
                flush=True,
            )

    def handle_utterance(self, pcm: bytes) -> bool:
        self.status.begin_asr()
        try:
            transcript = transcribe_wav(self.settings, pcm16_to_wav(pcm))
        except Exception as error:
            self.status.fail("asr", error)
            raise
        self.status.component_ok("asr")
        if not transcript:
            print("ASR returned no speech", flush=True)
            # An empty transcription does not consume the router's armed
            # follow-up utterance. Keep the published arm coherent with it.
            self.status.resume_listening()
            return False
        self.log_text("ASR utterance", transcript)
        heard_wake_word = self.router.wake_match(transcript) is not None
        command, state = self.router.route(transcript)
        if state == "ignored":
            print("Wake word absent; utterance ignored", flush=True)
            self.status.wake_not_detected()
            return False
        if state == "armed":
            print("Wake word heard; listening for one request", flush=True)
            self.status.wake_armed(self.settings.armed_seconds)
            return False
        assert command is not None
        self.status.wake_triggered(heard_wake_word)
        self.log_text("Voice request accepted", command)
        self.status.begin_openclaw()
        thinking_cue = self._start_thinking_cue()
        try:
            answer = ask_openclaw(self.settings, command)
        except Exception as error:
            self._cancel_playback(thinking_cue)
            self.status.fail("openclaw", error)
            raise
        # The cue normally finished while the model was thinking. Drain its
        # bounded final fraction before answer audio so the two never overlap.
        self._finish_thinking_cue(thinking_cue)
        if self.stop_event.is_set():
            return False
        self.status.component_ok("openclaw")
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
        self.status.begin_tts(len(chunks))
        if not chunks:
            self.status.finish_tts()
            self.status.ready()
            return False

        # Prime one chunk, then keep exactly one future chunk in RAM while the
        # current chunk plays in a separate aplay process. A single persistent,
        # cancellable Audio8 client process lets the coordinator observe stop
        # and playback failure promptly while both external processes run.
        try:
            synthesis_worker = self._get_synthesis_worker()
        except Exception as error:
            if self.stop_event.is_set():
                return False
            self.status.fail("tts_synthesis", error)
            raise
        self.status.tts_phase("synthesizing", 1)
        try:
            synthesis_worker.submit(chunks[0])
            next_wav: bytes | None = None
            while next_wav is None:
                if self.stop_event.is_set():
                    synthesis_worker.cancel()
                    return False
                next_wav = synthesis_worker.poll()
                if next_wav is None:
                    self.stop_event.wait(0.02)
        except Exception as error:
            if self.stop_event.is_set():
                return False
            synthesis_worker.cancel()
            self.status.fail("tts_synthesis", error)
            raise

        try:
            assert next_wav is not None
            for index in range(1, len(chunks) + 1):
                if self.stop_event.is_set():
                    return False
                self.status.tts_phase("playing", index)
                try:
                    current_wav = next_wav
                    next_wav = None
                    playback = self._start_playback(current_wav)
                    del current_wav
                except Exception as error:
                    if self.stop_event.is_set():
                        return False
                    self.status.fail("tts_playback", error)
                    raise

                if index == len(chunks):
                    try:
                        playback_completed = self._wait_playback(playback)
                    except Exception as error:
                        self.status.tts_phase("playing", index)
                        self.status.fail("tts_playback", error)
                        raise
                    if not playback_completed or self.stop_event.is_set():
                        return False
                    continue

                self.status.tts_phase("synthesizing", index + 1)
                synthesis_error: Exception | None = None
                try:
                    synthesis_worker.submit(chunks[index])
                except Exception as error:
                    synthesis_error = error

                playback_done = False
                while not playback_done or (
                    next_wav is None and synthesis_error is None
                ):
                    if self.stop_event.is_set():
                        synthesis_worker.cancel()
                        self._cancel_playback(playback)
                        return False

                    if next_wav is None and synthesis_error is None:
                        try:
                            completed_wav = synthesis_worker.poll()
                        except Exception as error:
                            synthesis_error = error
                        else:
                            if completed_wav is not None:
                                next_wav = completed_wav

                    if not playback_done:
                        try:
                            playback_done = playback.poll()
                        except Exception as error:
                            synthesis_worker.cancel()
                            self._release_playback(playback)
                            # Restore the diagnostic index to the chunk whose
                            # aplay process actually failed; synthesis may have
                            # already advanced the legacy current index.
                            self.status.tts_phase("playing", index)
                            self.status.fail("tts_playback", error)
                            raise

                    if not playback_done or (
                        next_wav is None and synthesis_error is None
                    ):
                        self.stop_event.wait(0.02)

                self._release_playback(playback)
                if synthesis_error is not None:
                    # The current sentence ended cleanly; the absent future
                    # sentence is now the only failed operation.
                    self.status.fail("tts_synthesis", synthesis_error)
                    raise synthesis_error
        finally:
            if self._active_playback is not None:
                self._cancel_playback()
            if synthesis_worker.busy:
                synthesis_worker.cancel()
            if synthesis_worker.closed:
                with self._synthesis_worker_lock:
                    if self._synthesis_worker is synthesis_worker:
                        self._synthesis_worker = None
        return bool(chunks)

    def capture_one(self) -> bool:
        vad = EnergyVad(self.settings)
        utterance: bytes | None = None
        try:
            self.recorder = Recorder(self.settings)
            while not self.stop_event.is_set():
                was_active = vad.active_frames is not None
                utterance = vad.feed(self.recorder.read_frame())
                if not was_active and vad.active_frames is not None:
                    self.status.speech_detected()
                elif was_active and vad.active_frames is None and utterance is None:
                    # A short impulse can enter VAD-active state and then be
                    # rejected for insufficient voiced frames. Return the
                    # dashboard to listening (or its still-live armed state)
                    # while capture continues.
                    self.status.resume_listening()
                if utterance is not None:
                    self.recorder.stop()
                    self.recorder = None
                    break
        except Exception as error:
            if not self.stop_event.is_set():
                self.status.fail("capture", error)
            raise
        finally:
            if self.recorder is not None:
                self.recorder.stop()
                self.recorder = None
        return self.handle_utterance(utterance) if utterance is not None else False

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.status.start()
        self._warm_thinking_cue()
        if self.stop_event.is_set():
            self.status.stop()
            return
        try:
            self._get_synthesis_worker()
        except Exception as error:
            # The first accepted request retries. Starting the microphone does
            # not depend on this optional latency optimization succeeding.
            print(
                "Audio8 client prestart failed: "
                f"{StatusPublisher._safe_error_type(error)}; will retry",
                flush=True,
            )
        self.status.ready()
        print(
            "Voice bridge ready; raw microphone audio and transcripts are not persisted",
            flush=True,
        )
        try:
            while not self.stop_event.is_set():
                try:
                    played = self.capture_one()
                    if played:
                        self.status.begin_cooldown()
                        if self.stop_event.wait(
                            self.settings.playback_cooldown_seconds
                        ):
                            break
                        self.status.finish_tts()
                        self.status.ready()
                except urllib.error.HTTPError as error:
                    print(
                        f"Local voice API returned HTTP {error.code}; retrying",
                        flush=True,
                    )
                    if not self.stop_event.wait(2):
                        self.status.resume_listening()
                except urllib.error.URLError:
                    print("Local voice API is unavailable; retrying", flush=True)
                    if not self.stop_event.wait(2):
                        self.status.resume_listening()
                except Exception as error:
                    if not self.stop_event.is_set():
                        print(
                            f"Voice bridge error: {type(error).__name__}; retrying",
                            flush=True,
                        )
                        if not self.stop_event.wait(2):
                            self.status.resume_listening()
        finally:
            self._close_synthesis_worker()
            self.status.stop()


def main() -> None:
    VoiceBridge(Settings.from_environment()).run()


if __name__ == "__main__":
    main()
