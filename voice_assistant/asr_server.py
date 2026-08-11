#!/usr/bin/env python3
"""Loopback-only Qwen3-ASR inference service for the C3 voice bridge."""

from __future__ import annotations

import io
import ipaddress
import json
import math
import os
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MODEL_PATH = os.environ.get("QWEN_ASR_MODEL_PATH", "/models/qwen-asr")
MODEL_NAME = os.environ.get("QWEN_ASR_MODEL_NAME", "qwen/qwen3-asr-1.7b")
LISTEN_HOST = os.environ.get("QWEN_ASR_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("QWEN_ASR_PORT", "8020"))
LANGUAGE = os.environ.get("QWEN_ASR_LANGUAGE", "en").strip() or None
VOCABULARY_PROMPT = os.environ.get(
    "QWEN_ASR_VOCABULARY_PROMPT",
    "Vocabulary: Cerberus, Cerberus One, Cerberus Two, Cerberus Three, "
    "cerebrus1, cerebrus2, cerebrus3.",
).strip()
MAX_AUDIO_SECONDS = float(os.environ.get("QWEN_ASR_MAX_AUDIO_SECONDS", "35"))
MIN_AUDIO_SECONDS = float(os.environ.get("QWEN_ASR_MIN_AUDIO_SECONDS", "0.15"))
MAX_NEW_TOKENS = int(os.environ.get("QWEN_ASR_MAX_NEW_TOKENS", "256"))
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
INFERENCE_LOCK = threading.Lock()
RUNTIME: "QwenAsrRuntime | None" = None


def validate_configuration() -> None:
    """Reject accidental public exposure and nonsensical resource limits."""
    try:
        address = ipaddress.ip_address(LISTEN_HOST)
    except ValueError as error:
        raise RuntimeError("QWEN_ASR_HOST must be a numeric loopback address") from error
    if not address.is_loopback:
        raise RuntimeError("QWEN_ASR_HOST must be a loopback address")
    if not 1 <= LISTEN_PORT <= 65_535:
        raise RuntimeError("QWEN_ASR_PORT must be between 1 and 65535")
    if not math.isfinite(MIN_AUDIO_SECONDS) or not 0.05 <= MIN_AUDIO_SECONDS <= 5:
        raise RuntimeError("QWEN_ASR_MIN_AUDIO_SECONDS must be between 0.05 and 5")
    if (
        not math.isfinite(MAX_AUDIO_SECONDS)
        or not 1 <= MAX_AUDIO_SECONDS <= 300
        or MAX_AUDIO_SECONDS <= MIN_AUDIO_SECONDS
    ):
        raise RuntimeError("QWEN_ASR_MAX_AUDIO_SECONDS must be between 1 and 300")
    if not 16 <= MAX_NEW_TOKENS <= 2048:
        raise RuntimeError("QWEN_ASR_MAX_NEW_TOKENS must be between 16 and 2048")
    validate_vocabulary_prompt(VOCABULARY_PROMPT)


def validate_vocabulary_prompt(value: str) -> str:
    """Keep the ASR system prompt short, single-line, and operator-controlled."""
    if not 1 <= len(value) <= 512:
        raise RuntimeError(
            "QWEN_ASR_VOCABULARY_PROMPT must contain 1-512 characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RuntimeError(
            "QWEN_ASR_VOCABULARY_PROMPT cannot contain control characters"
        )
    return value


def prepare_transcription_request(processor: Any, audio: Any) -> Any:
    return processor.apply_transcription_request(
        audio=audio,
        language=LANGUAGE,
        prompt=VOCABULARY_PROMPT,
    )


def decode_pcm16_wav(payload: bytes) -> tuple[bytes, float]:
    """Validate the wire format and return raw mono PCM samples."""
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise ValueError("body must be a RIFF/WAVE file")
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getcomptype() != "NONE":
                raise ValueError("compressed WAV is not supported")
            if source.getnchannels() != CHANNELS:
                raise ValueError("WAV must be mono")
            if source.getsampwidth() != SAMPLE_WIDTH:
                raise ValueError("WAV must contain signed 16-bit PCM")
            if source.getframerate() != SAMPLE_RATE:
                raise ValueError("WAV sample rate must be 16000 Hz")
            frames = source.getnframes()
            duration = frames / SAMPLE_RATE
            if not MIN_AUDIO_SECONDS <= duration <= MAX_AUDIO_SECONDS:
                raise ValueError(
                    f"WAV duration must be {MIN_AUDIO_SECONDS:g}-"
                    f"{MAX_AUDIO_SECONDS:g} seconds"
                )
            raw = source.readframes(frames)
            if len(raw) != frames * SAMPLE_WIDTH:
                raise ValueError("WAV payload is truncated")
    except (EOFError, wave.Error) as error:
        raise ValueError("body is not a valid PCM WAV file") from error

    return raw, duration


class QwenAsrRuntime:
    def __init__(self) -> None:
        import numpy as np
        import torch
        from transformers import AutoModelForMultimodalLM, AutoProcessor

        if not torch.cuda.is_available():
            raise RuntimeError("Qwen3-ASR requires a CUDA GPU")
        self.np = np
        self.torch = torch
        torch.backends.cuda.matmul.allow_tf32 = True
        self.device = torch.device("cuda:0")
        self.dtype = torch.bfloat16
        self.processor = AutoProcessor.from_pretrained(
            MODEL_PATH,
            local_files_only=True,
        )
        self.model = AutoModelForMultimodalLM.from_pretrained(
            MODEL_PATH,
            local_files_only=True,
            dtype=self.dtype,
            attn_implementation="sdpa",
        ).eval().to(self.device)

    def transcribe(self, pcm16: bytes) -> tuple[str, str | None, float]:
        audio = self.np.frombuffer(pcm16, dtype="<i2").astype(self.np.float32)
        audio *= 1.0 / 32768.0
        inputs = prepare_transcription_request(self.processor, audio).to(
            self.device, self.dtype
        )
        started = time.monotonic()
        with INFERENCE_LOCK, self.torch.inference_mode():
            output_ids = self.model.generate(
                **inputs,
                max_new_tokens=MAX_NEW_TOKENS,
                do_sample=False,
            )
        elapsed = time.monotonic() - started
        generated_ids = output_ids[:, inputs["input_ids"].shape[1] :]
        parsed = self.processor.decode(
            generated_ids,
            return_format="parsed",
        )[0]
        text = parsed.get("transcription", "")
        language = parsed.get("language")
        if not isinstance(text, str):
            raise RuntimeError("ASR processor returned an invalid transcription")
        if language is not None and not isinstance(language, str):
            language = None
        return text.strip(), language, elapsed


class Handler(BaseHTTPRequestHandler):
    server_version = "CerberusQwenASR/1"

    def log_message(self, format_string: str, *args: Any) -> None:
        # Never emit request bodies or transcriptions.
        print(f"{self.client_address[0]} {format_string % args}", flush=True)

    def json_response(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def client_is_loopback(self) -> bool:
        try:
            return ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self.client_is_loopback():
            self.json_response(403, {"error": "loopback clients only"})
            return
        if self.path == "/health":
            self.json_response(
                200,
                {
                    "status": "ok" if RUNTIME is not None else "loading",
                    "model": MODEL_NAME,
                    "precision": "bfloat16",
                    "sample_rate": SAMPLE_RATE,
                },
            )
            return
        self.json_response(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self.client_is_loopback():
            self.json_response(403, {"error": "loopback clients only"})
            return
        if self.path != "/transcribe":
            self.json_response(404, {"error": "not found"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type not in {"audio/wav", "audio/x-wav"}:
            self.json_response(415, {"error": "Content-Type must be audio/wav"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.json_response(400, {"error": "chunked requests are not supported"})
            return
        maximum_bytes = int(MAX_AUDIO_SECONDS * SAMPLE_RATE * SAMPLE_WIDTH) + 65_536
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 44 <= content_length <= maximum_bytes:
            self.json_response(413, {"error": "invalid audio request size"})
            return
        try:
            pcm16, duration = decode_pcm16_wav(self.rfile.read(content_length))
            if RUNTIME is None:
                self.json_response(503, {"error": "ASR model is loading"})
                return
            text, language, elapsed = RUNTIME.transcribe(pcm16)
        except ValueError as error:
            self.json_response(400, {"error": str(error)})
            return
        except Exception as error:  # Keep model and transcript details private.
            print(f"transcription failed: {type(error).__name__}", flush=True)
            self.json_response(500, {"error": "transcription failed"})
            return
        self.json_response(
            200,
            {
                "text": text,
                "language": language,
                "duration_seconds": round(duration, 3),
                "inference_seconds": round(elapsed, 3),
            },
        )


def main() -> None:
    global RUNTIME
    validate_configuration()
    print(f"Loading {MODEL_NAME} from {MODEL_PATH}", flush=True)
    RUNTIME = QwenAsrRuntime()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    print(f"Qwen3-ASR listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
