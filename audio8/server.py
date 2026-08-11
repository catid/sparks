#!/usr/bin/env python3
"""Small, single-GPU OpenAI-compatible Audio8 TTS server for Cerberus node 3."""

from __future__ import annotations

import ctypes
import io
import json
import math
import os
import pathlib
import subprocess
import tempfile
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
REQUESTED_SDPA_BACKEND = os.environ.get(
    "AUDIO8_SDPA_BACKEND", "efficient"
).strip().lower()


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value == "1":
        return True
    if value == "0":
        return False
    raise RuntimeError(f"{name} must be 0 or 1")


COMPILE_CODEBOOKS = environment_flag("AUDIO8_COMPILE_CODEBOOKS")
INFERENCE_LOCK = threading.Lock()
if not 1 <= MAX_ACTIVE_REQUESTS <= 32:
    raise RuntimeError("AUDIO8_MAX_ACTIVE_REQUESTS must be between 1 and 32")
REQUEST_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE_REQUESTS)


def select_sdpa_backend(model: Any, requested: str) -> str:
    """Select the pinned model's slow-AR SDPA kernel without editing its files.

    Audio8's remote code explicitly requests the math backend around every
    autoregressive slow-model step.  PyTorch's fused efficient backend accepts
    that exact boolean-mask shape on GB10 and computes the same SDPA operation
    without changing model weights, BF16, sampling, or codec settings.
    """
    if requested not in {"math", "efficient"}:
        raise RuntimeError("AUDIO8_SDPA_BACKEND must be math or efficient")
    generate = getattr(model, "generate", None)
    function = getattr(generate, "__func__", generate)
    # The pinned method is decorated with torch.inference_mode(), whose public
    # wrapper has torch's globals rather than the remote model module's globals.
    # Follow the standard __wrapped__ chain to the function that actually
    # references sdpa_kernel/SDPBackend.
    seen: set[int] = set()
    while callable(function) and callable(getattr(function, "__wrapped__", None)):
        identity = id(function)
        if identity in seen:
            raise RuntimeError("Audio8 generate wrapper chain is cyclic")
        seen.add(identity)
        function = function.__wrapped__
    namespace = getattr(function, "__globals__", None)
    original_key = "_cerberus_original_sdpa_kernel"
    if requested == "math":
        # Math is already the pinned upstream behavior. If this model was
        # previously patched in-process, restore it; otherwise no symbols need
        # to be discoverable merely to retain the default.
        if isinstance(namespace, dict) and original_key in namespace:
            namespace["sdpa_kernel"] = namespace[original_key]
        return "math"
    if not isinstance(namespace, dict):
        raise RuntimeError("Audio8 generate implementation is not patchable")
    kernel = namespace.get("sdpa_kernel")
    backends = namespace.get("SDPBackend")
    if not callable(kernel) or backends is None:
        raise RuntimeError("Audio8 SDPA symbols are unavailable")
    original = namespace.setdefault(original_key, kernel)
    efficient = getattr(backends, "EFFICIENT_ATTENTION", None)
    if efficient is None:
        raise RuntimeError("PyTorch efficient SDPA is unavailable")

    def efficient_kernel(_upstream_backend: Any):
        return original(efficient)

    namespace["sdpa_kernel"] = efficient_kernel
    return "efficient"


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
        self.sdpa_backend = select_sdpa_backend(
            self.model, REQUESTED_SDPA_BACKEND
        )
        self.codebook_compile_requested = COMPILE_CODEBOOKS
        self.codebook_compile_active = False
        self.codebook_compile_seconds: float | None = None
        self.eager_generate_codebooks = getattr(
            self.model, "_generate_codebooks", None
        )
        self.compiled_generate_codebooks: Any | None = None
        self.sample_rate = int(self.model.config.codec_sample_rate)
        if bool(REFERENCE_AUDIO) != bool(REFERENCE_TEXT_FILE):
            raise RuntimeError(
                "AUDIO8_REFERENCE_AUDIO and AUDIO8_REFERENCE_TEXT_FILE "
                "must be configured together"
            )
        self.reference_audio: str | None = REFERENCE_AUDIO or None
        self.reference_text: str | None = None
        self.reference_codes: torch.Tensor | None = None
        if REFERENCE_TEXT_FILE:
            with open(REFERENCE_TEXT_FILE, encoding="utf-8") as source:
                reference_text = source.read(4097)
            if not reference_text.strip() or len(reference_text) > 4096:
                raise RuntimeError("reference transcript must contain 1-4096 characters")
            self.reference_text = reference_text.strip()
        if self.reference_audio and self.reference_text:
            self.reference_codes = self._encode_reference_once()
        if self.codebook_compile_requested:
            self._compile_and_prewarm_codebooks()

    @staticmethod
    def _verify_executable_compile_cache() -> None:
        """Prove the exact Inductor cache mount can load native code."""
        cache_text = os.environ.get("TORCHINDUCTOR_CACHE_DIR", "").strip()
        if not cache_text:
            raise RuntimeError("TORCHINDUCTOR_CACHE_DIR is required")
        cache = pathlib.Path(cache_text)
        if not cache.is_dir() or cache.is_symlink():
            raise RuntimeError("Inductor cache is not a regular directory")
        with tempfile.TemporaryDirectory(prefix=".exec-check-", dir=cache) as root:
            directory = pathlib.Path(root)
            source = directory / "probe.c"
            library = directory / "probe.so"
            source.write_text(
                "int cerberus_audio8_cache_probe(void) { return 8; }\n",
                encoding="ascii",
            )
            subprocess.run(
                ["cc", "-shared", "-fPIC", "-O0", "-o", str(library), str(source)],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            probe = ctypes.CDLL(str(library))
            function = probe.cerberus_audio8_cache_probe
            function.restype = ctypes.c_int
            if function() != 8:
                raise RuntimeError("executable cache probe returned the wrong value")

    def _compile_and_prewarm_codebooks(self) -> None:
        """Compile only Audio8's stock fast-codebook method before API bind.

        Any failure restores the exact eager bound method. The surrounding
        generation loop, BF16 weights, sampling settings, reference voice, and
        codec remain unchanged.
        """
        original = self.eager_generate_codebooks
        if not callable(original):
            print(
                "Audio8 codebook compile unavailable (missing method); using eager",
                flush=True,
            )
            return
        started = time.monotonic()
        try:
            self._verify_executable_compile_cache()
            compiled = torch.compile(original)
            self.model._generate_codebooks = compiled
            inputs = self.processor(
                **self.processor_inputs("."), return_tensors="pt"
            )
            inputs = {
                name: value.to(self.device) for name, value in inputs.items()
            }
            with INFERENCE_LOCK, torch.inference_mode():
                torch.manual_seed(260810)
                self.model.generate(
                    **inputs,
                    max_new_tokens=1,
                    temperature=0.8,
                    top_p=0.95,
                    top_k=50,
                    do_sample=True,
                    return_dict_in_generate=True,
                )
                torch.cuda.synchronize()
        except Exception as error:
            self.model._generate_codebooks = original
            self.compiled_generate_codebooks = None
            print(
                "Audio8 codebook compile unavailable "
                f"({type(error).__name__}); using eager",
                flush=True,
            )
            return
        self.model._generate_codebooks = original
        self.compiled_generate_codebooks = compiled
        self.codebook_compile_active = True
        self.codebook_compile_seconds = time.monotonic() - started
        print(
            "Audio8 stock codebook generator compiled and prewarmed in "
            f"{self.codebook_compile_seconds:.3f}s",
            flush=True,
        )

    def _encode_reference_once(self) -> torch.Tensor:
        """Keep the operator-approved voice conditioning in RAM across requests."""
        assert self.reference_audio and self.reference_text
        prepared = self.processor(
            text=["."],
            reference_audio=[self.reference_audio],
            reference_text=[self.reference_text],
            return_tensors="pt",
        )
        audio_values = prepared["reference_audio_values"].to(self.device)
        audio_lengths = prepared["reference_audio_lengths"].to(self.device)
        with torch.inference_mode():
            codes, lengths = self.model.encode_audio(audio_values, audio_lengths)
        length = int(lengths[0])
        if length <= 0:
            raise RuntimeError("reference voice produced no conditioning frames")
        # The processor accepts a small CPU code tensor and moves its request
        # batch to the GPU below. No voice data is written to persistent storage.
        return codes[0, :, :length].long().cpu().contiguous()

    def processor_inputs(self, text: str) -> dict[str, Any]:
        inputs: dict[str, Any] = {"text": [text]}
        if self.reference_codes is not None and self.reference_text:
            inputs.update(
                {
                    "reference_codes": self.reference_codes,
                    "reference_text": [self.reference_text],
                }
            )
        return inputs

    def codebook_generator_for(
        self, temperature: float, top_p: float, top_k: int
    ) -> tuple[Any, bool]:
        compiled = self.compiled_generate_codebooks
        use_compiled = bool(
            self.codebook_compile_active
            and compiled is not None
            and temperature == 0.8
            and top_p == 0.95
            and top_k == 50
        )
        return (
            compiled if use_compiled else self.eager_generate_codebooks,
            use_compiled,
        )

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

        inputs = self.processor(
            **self.processor_inputs(text), return_tensors="pt"
        )
        inputs = {name: value.to(self.device) for name, value in inputs.items()}
        started = time.monotonic()
        with INFERENCE_LOCK, torch.inference_mode():
            # Select only after taking the same lock that protects inference and
            # compile-fallback mutation. A queued request must observe an eager
            # fallback if the preceding compiled request disabled its wrapper.
            selected_codebooks, use_compiled_codebooks = self.codebook_generator_for(
                temperature, top_p, top_k
            )
            torch.manual_seed(seed)
            previous_codebooks = self.model._generate_codebooks
            self.model._generate_codebooks = selected_codebooks
            try:
                output = self.model.generate(
                    **inputs,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    top_k=top_k,
                    do_sample=True,
                    return_dict_in_generate=True,
                )
            except Exception:
                if use_compiled_codebooks:
                    self.codebook_compile_active = False
                    self.compiled_generate_codebooks = None
                raise
            finally:
                self.model._generate_codebooks = previous_codebooks
            waveforms, lengths = self.model.decode_audio(output.codes)
            audio = waveforms[0, : int(lengths[0])].float().cpu().numpy()
        elapsed = time.monotonic() - started

        encoded = io.BytesIO()
        sf.write(encoded, audio, self.sample_rate, format="WAV", subtype="PCM_16")
        return encoded.getvalue(), elapsed


RUNTIME: Audio8Runtime | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "CerberusAudio8/1"

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
                    "reference_conditioning_cached": bool(
                        RUNTIME and RUNTIME.reference_codes is not None
                    ),
                    "sample_rate": RUNTIME.sample_rate if RUNTIME else None,
                    "sdpa_backend": RUNTIME.sdpa_backend if RUNTIME else None,
                    "codebook_compile_requested": bool(
                        RUNTIME and RUNTIME.codebook_compile_requested
                    ),
                    "codebook_compile_active": bool(
                        RUNTIME and RUNTIME.codebook_compile_active
                    ),
                    "codebook_compile_state": (
                        "compiled"
                        if RUNTIME and RUNTIME.codebook_compile_active
                        else "eager_fallback"
                        if RUNTIME and RUNTIME.codebook_compile_requested
                        else "eager"
                    ),
                    "codebook_compile_seconds": (
                        RUNTIME.codebook_compile_seconds if RUNTIME else None
                    ),
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
    print(
        f"Audio8 listening on {LISTEN_HOST}:{LISTEN_PORT} "
        f"with {RUNTIME.sdpa_backend} SDPA and "
        f"{'compiled' if RUNTIME.codebook_compile_active else 'eager'} codebooks",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
