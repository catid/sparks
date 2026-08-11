#!/usr/bin/env python3
"""Small, single-GPU OpenAI-compatible Audio8 TTS server for Cerebrus 3."""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import soundfile as sf
import torch
from transformers import AutoModel, AutoProcessor


MODEL_PATH = os.environ.get("AUDIO8_MODEL_PATH", "/models/audio8")
MODEL_NAME = os.environ.get("AUDIO8_MODEL_NAME", "audio8/tts-0.6b")
LISTEN_HOST = os.environ.get("AUDIO8_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("AUDIO8_PORT", "8010"))
MAX_INPUT_CHARACTERS = int(os.environ.get("AUDIO8_MAX_INPUT_CHARACTERS", "300"))
MAX_ACTIVE_REQUESTS = int(os.environ.get("AUDIO8_MAX_ACTIVE_REQUESTS", "2"))
REFERENCE_AUDIO = os.environ.get("AUDIO8_REFERENCE_AUDIO", "").strip()
REFERENCE_TEXT_FILE = os.environ.get("AUDIO8_REFERENCE_TEXT_FILE", "").strip()
INFERENCE_LOCK = threading.Lock()
if not 1 <= MAX_ACTIVE_REQUESTS <= 32:
    raise RuntimeError("AUDIO8_MAX_ACTIVE_REQUESTS must be between 1 and 32")
REQUEST_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_REQUESTS)


def finite_number(
    value: Any, default: float, minimum: float, maximum: float
) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be a number") from error
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"value must be between {minimum} and {maximum}")
    return parsed


def integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("boolean is not an integer")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("value must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"value must be between {minimum} and {maximum}")
    return parsed


class Audio8Runtime:
    def __init__(self) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("Audio8 requires a CUDA GPU")
        torch.backends.cuda.matmul.allow_tf32 = True
        self.device = torch.device("cuda:0")
        self.dtype = torch.bfloat16
        self.processor = AutoProcessor.from_pretrained(
            MODEL_PATH,
            trust_remote_code=True,
            local_files_only=True,
        )
        self.model = AutoModel.from_pretrained(
            MODEL_PATH,
            trust_remote_code=True,
            local_files_only=True,
            dtype=self.dtype,
        ).eval().to(self.device)
        self.sample_rate = int(self.model.config.codec_sample_rate)
        if bool(REFERENCE_AUDIO) != bool(REFERENCE_TEXT_FILE):
            raise RuntimeError(
                "AUDIO8_REFERENCE_AUDIO and AUDIO8_REFERENCE_TEXT_FILE "
                "must be configured together"
            )
        self.reference_audio: str | None = REFERENCE_AUDIO or None
        self.reference_text: str | None = None
        if REFERENCE_TEXT_FILE:
            with open(REFERENCE_TEXT_FILE, encoding="utf-8") as source:
                reference_text = source.read(4097)
            if not reference_text.strip() or len(reference_text) > 4096:
                raise RuntimeError("reference transcript must contain 1-4096 characters")
            self.reference_text = reference_text.strip()

    def synthesize(self, request: dict[str, Any]) -> tuple[bytes, float]:
        text = request.get("input")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be a non-empty string")
        text = text.strip()
        if len(text) > MAX_INPUT_CHARACTERS:
            raise ValueError(
                f"input exceeds {MAX_INPUT_CHARACTERS} characters; split long text"
            )
        if request.get("model", MODEL_NAME) != MODEL_NAME:
            raise ValueError(f"model must be {MODEL_NAME}")
        if request.get("response_format", "wav") != "wav":
            raise ValueError("only response_format=wav is supported")
        forbidden_reference_fields = {
            "references",
            "reference_audio",
            "reference_audio_path",
            "reference_text",
            "reference_text_file",
        }
        if forbidden_reference_fields.intersection(request):
            raise ValueError("client-supplied voice references are not supported")

        max_new_tokens = integer(request.get("max_new_tokens"), 512, 32, 2048)
        temperature = finite_number(request.get("temperature"), 0.8, 0.05, 2.0)
        top_p = finite_number(request.get("top_p"), 0.95, 0.05, 1.0)
        top_k = integer(request.get("top_k"), 50, 1, 4096)
        seed = integer(request.get("seed"), 260810, 0, 2**31 - 1)

        processor_inputs: dict[str, Any] = {"text": [text]}
        if self.reference_audio and self.reference_text:
            processor_inputs.update(
                {
                    "reference_audio": [self.reference_audio],
                    "reference_text": [self.reference_text],
                }
            )
        inputs = self.processor(**processor_inputs, return_tensors="pt")
        inputs = {name: value.to(self.device) for name, value in inputs.items()}
        started = time.monotonic()
        with INFERENCE_LOCK, torch.inference_mode():
            torch.manual_seed(seed)
            output = self.model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                do_sample=True,
                return_dict_in_generate=True,
            )
            waveforms, lengths = self.model.decode_audio(output.codes)
            audio = waveforms[0, : int(lengths[0])].float().cpu().numpy()
        elapsed = time.monotonic() - started

        encoded = io.BytesIO()
        sf.write(encoded, audio, self.sample_rate, format="WAV", subtype="PCM_16")
        return encoded.getvalue(), elapsed


RUNTIME: Audio8Runtime | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "CerebrusAudio8/1"

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.client_address[0]} {format_string % args}", flush=True)

    def json_response(
        self,
        status: int,
        payload: dict[str, Any],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path == "/health":
            self.json_response(
                200,
                {
                    "status": "ok",
                    "model": MODEL_NAME,
                    "synthetic_audio": True,
                    "reference_conditioned": bool(
                        RUNTIME and RUNTIME.reference_audio
                    ),
                    "sample_rate": RUNTIME.sample_rate if RUNTIME else None,
                },
            )
            return
        self.json_response(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/audio/speech":
            self.json_response(404, {"error": "not found"})
            return
        if not REQUEST_SLOTS.acquire(blocking=False):
            self.json_response(
                429,
                {"error": "Audio8 request queue is full; retry later"},
                {"Retry-After": "5"},
            )
            return
        try:
            self.handle_speech_request()
        finally:
            REQUEST_SLOTS.release()

    def handle_speech_request(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= content_length <= 32_768:
                raise ValueError("invalid request size")
            request = json.loads(self.rfile.read(content_length))
            if not isinstance(request, dict):
                raise ValueError("JSON body must be an object")
            assert RUNTIME is not None
            audio, elapsed = RUNTIME.synthesize(request)
        except (AssertionError, json.JSONDecodeError, ValueError) as error:
            self.json_response(400, {"error": str(error)})
            return
        except Exception as error:  # Keep model internals out of HTTP responses.
            print(f"synthesis failed: {type(error).__name__}: {error}", flush=True)
            self.json_response(500, {"error": "synthesis failed"})
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Audio8-Synthetic", "true")
        self.send_header("X-Synthesis-Seconds", f"{elapsed:.3f}")
        self.end_headers()
        self.wfile.write(audio)


def main() -> None:
    global RUNTIME
    print(f"Loading {MODEL_NAME} from {MODEL_PATH}", flush=True)
    RUNTIME = Audio8Runtime()
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"Audio8 listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
