#!/usr/bin/env python3
"""Hardened fixed-reference gateway for the experimental Audio8 SGLang API."""

from __future__ import annotations

import io
import http.client
import ipaddress
import json
import math
import os
import socket
import threading
import time
import urllib.parse
import wave
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MAX_HTTP_BODY = 32_768
MAX_WAV_BYTES = 32 * 1024 * 1024
EXPERIMENTAL_BACKEND_URL = "http://127.0.0.1:18010/v1/audio/speech"
EXPERIMENTAL_GATEWAY_HOST = "127.0.0.1"
EXPERIMENTAL_GATEWAY_PORT = 18_011
ALLOWED_FIELDS = {
    "input",
    "max_new_tokens",
    "model",
    "response_format",
    "seed",
    "speed",
    "temperature",
    "top_k",
    "top_p",
    "voice",
}
FORBIDDEN_REFERENCE_FIELDS = {
    "ref_audio",
    "ref_text",
    "reference_audio",
    "reference_audio_path",
    "reference_text",
    "reference_text_file",
    "references",
}


def bounded_integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or (
        isinstance(value, float) and not value.is_integer()
    ):
        raise ValueError("value must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("value must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise ValueError(f"value must be between {minimum} and {maximum}")
    return parsed


def bounded_number(
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


def positive_environment_integer(name: str, default: int, maximum: int) -> int:
    return bounded_integer(os.environ.get(name), default, 1, maximum)


def positive_environment_number(name: str, default: float, maximum: float) -> float:
    return bounded_number(os.environ.get(name), default, 0.1, maximum)


def loopback_http_url(value: str, required_path: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != required_path
    ):
        raise RuntimeError(f"backend URL must be loopback HTTP {required_path}")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as error:
        raise RuntimeError("backend URL must use a numeric loopback address") from error
    if not address.is_loopback or parsed.port is None:
        raise RuntimeError("backend URL must use an explicit loopback port")
    return value


@dataclass(frozen=True)
class Settings:
    backend_url: str
    backend_health_url: str
    listen_host: str
    listen_port: int
    model_name: str
    max_input_characters: int
    max_active_requests: int
    max_connections: int
    header_timeout_seconds: float
    body_timeout_seconds: float
    write_timeout_seconds: float
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "Settings":
        backend_url = os.environ.get(
            "AUDIO8_SGLANG_BACKEND_URL", EXPERIMENTAL_BACKEND_URL
        )
        loopback_http_url(backend_url, "/v1/audio/speech")
        if backend_url != EXPERIMENTAL_BACKEND_URL:
            raise RuntimeError(
                "experimental backend is fixed to 127.0.0.1:18010"
            )
        parsed = urllib.parse.urlsplit(backend_url)
        backend_health_url = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, "/health", "", "")
        )
        listen_host = os.environ.get(
            "AUDIO8_SGLANG_GATEWAY_HOST", EXPERIMENTAL_GATEWAY_HOST
        )
        if listen_host != EXPERIMENTAL_GATEWAY_HOST:
            raise RuntimeError("experimental gateway is fixed to 127.0.0.1")
        listen_port = positive_environment_integer(
            "AUDIO8_SGLANG_GATEWAY_PORT", EXPERIMENTAL_GATEWAY_PORT, 65_535
        )
        if listen_port != EXPERIMENTAL_GATEWAY_PORT:
            raise RuntimeError("experimental gateway is fixed to port 18011")
        return cls(
            backend_url=backend_url,
            backend_health_url=backend_health_url,
            listen_host=listen_host,
            listen_port=listen_port,
            model_name=os.environ.get(
                "AUDIO8_SGLANG_MODEL_NAME", "audio8/tts-0.6b"
            ),
            max_input_characters=positive_environment_integer(
                "AUDIO8_SGLANG_MAX_INPUT_CHARACTERS", 300, 4096
            ),
            max_active_requests=positive_environment_integer(
                "AUDIO8_SGLANG_MAX_ACTIVE_REQUESTS", 2, 8
            ),
            max_connections=positive_environment_integer(
                "AUDIO8_SGLANG_MAX_CONNECTIONS", 16, 64
            ),
            header_timeout_seconds=positive_environment_number(
                "AUDIO8_SGLANG_HEADER_TIMEOUT_SECONDS", 5, 30
            ),
            body_timeout_seconds=positive_environment_number(
                "AUDIO8_SGLANG_BODY_TIMEOUT_SECONDS", 5, 30
            ),
            write_timeout_seconds=positive_environment_number(
                "AUDIO8_SGLANG_WRITE_TIMEOUT_SECONDS", 30, 120
            ),
            timeout_seconds=positive_environment_number(
                "AUDIO8_SGLANG_TIMEOUT_SECONDS", 240, 900
            ),
        )


def validate_backend_attestation(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("backend runtime attestation is missing")
    boolean_fields = (
        "engine_initialized",
        "fixed_reference_configured",
        "fixed_reference_cached",
        "cuda_graph_requested",
        "cuda_graph_active",
        "torch_compile_requested",
        "torch_compile_active",
        "single_process_attested",
        "ready",
    )
    if type(payload.get("schema")) is not int or payload["schema"] != 1 or any(
        type(payload.get(field)) is not bool for field in boolean_fields
    ):
        raise RuntimeError("backend runtime attestation is invalid")
    if (
        payload["ready"] is not True
        or payload["single_process_attested"] is not True
        or payload["engine_initialized"] is not True
        or payload["fixed_reference_configured"] is not True
        or payload["fixed_reference_cached"] is not True
        or payload.get("slow_attention_backend") != "flashinfer"
        or payload.get("fast_attention_backend") != "flashinfer"
    ):
        raise RuntimeError("backend runtime attestation is not production-safe")
    process_ids = tuple(
        payload.get(field)
        for field in (
            "runtime_process_id",
            "engine_process_id",
            "reference_process_id",
        )
    )
    if (
        any(type(process_id) is not int or process_id <= 0 for process_id in process_ids)
        or len(set(process_ids)) != 1
    ):
        raise RuntimeError("backend process attestation is inconsistent")
    graph_batches = payload.get("cuda_graph_batches")
    batches = payload.get("torch_compile_batches")
    for label, values in (
        ("CUDA Graph", graph_batches),
        ("compile", batches),
    ):
        if (
            not isinstance(values, list)
            or any(type(batch) is not int for batch in values)
            or any(not 1 <= batch <= 8 for batch in values)
            or values != sorted(set(values))
        ):
            raise RuntimeError(f"backend {label} batch attestation is invalid")
    if payload["cuda_graph_active"] is not bool(graph_batches):
        raise RuntimeError("backend CUDA Graph attestation is inconsistent")
    if payload["cuda_graph_active"] and not payload["cuda_graph_requested"]:
        raise RuntimeError("backend CUDA Graph attestation is inconsistent")
    if not set(batches).issubset(graph_batches):
        raise RuntimeError("backend compile batch attestation is inconsistent")
    if payload["torch_compile_active"] and (
        not payload["torch_compile_requested"]
        or not payload["cuda_graph_active"]
        or not batches
    ):
        raise RuntimeError("backend compile attestation is inconsistent")
    return dict(payload)


class GatewayRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.request_slots = threading.BoundedSemaphore(
            settings.max_active_requests
        )

    @staticmethod
    def _abort_connection(connection: http.client.HTTPConnection) -> None:
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()

    @staticmethod
    def _read_backend_body(
        response: http.client.HTTPResponse,
        connection: http.client.HTTPConnection,
        maximum_bytes: int,
        deadline: float,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while total <= maximum_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("backend response exceeded its total deadline")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65_536, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)

    def request_backend(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
        maximum_bytes: int,
        timeout_seconds: float,
    ) -> tuple[http.client.HTTPMessage, bytes, float]:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme != "http" or host is None or port is None:
            raise RuntimeError("backend URL is invalid")
        started = time.monotonic()
        deadline = started + timeout_seconds
        connection = http.client.HTTPConnection(
            host, port, timeout=timeout_seconds
        )
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            self._abort_connection(connection)

        timer = threading.Timer(timeout_seconds, expire)
        timer.daemon = True
        timer.start()
        try:
            connection.request(method, parsed.path, body=body, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("backend rejected request")
            data = self._read_backend_body(
                response, connection, maximum_bytes, deadline
            )
        except (OSError, TimeoutError, http.client.HTTPException) as error:
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError(
                    "backend request exceeded its total deadline"
                ) from error
            raise RuntimeError("backend request failed") from error
        finally:
            timer.cancel()
            self._abort_connection(connection)
        if expired.is_set() or time.monotonic() > deadline:
            raise TimeoutError("backend request exceeded its total deadline")
        return response.headers, data, time.monotonic() - started

    def backend_health(self) -> tuple[dict[str, Any], dict[str, Any]]:
        headers, data, _elapsed = self.request_backend(
            "GET",
            self.settings.backend_health_url,
            body=None,
            headers={
                "Accept": "application/json",
                "User-Agent": "CerberusAudio8Gateway/1",
            },
            maximum_bytes=65_536,
            timeout_seconds=3,
        )
        if len(data) > 65_536:
            raise RuntimeError("backend health response is too large")
        if headers.get_content_type() != "application/json":
            raise RuntimeError("backend health content type is invalid")
        payload = json.loads(data)
        if (
            not isinstance(payload, dict)
            or payload.get("status") != "healthy"
            or payload.get("running") is not True
        ):
            raise RuntimeError("backend is not healthy")
        attestation = validate_backend_attestation(payload.get("audio8_runtime"))
        return payload, attestation

    def health_document(self) -> tuple[int, dict[str, Any]]:
        try:
            backend, attestation = self.backend_health()
        except Exception:
            return 503, {
                "status": "loading",
                "model": self.settings.model_name,
                "synthetic_audio": True,
                "reference_conditioned": True,
                "reference_conditioning_cached": False,
                "sample_rate": 44_100,
                "backend": "sglang-omni",
            }
        return 200, {
            "status": "ok",
            "model": self.settings.model_name,
            "synthetic_audio": True,
            "reference_conditioned": True,
            "reference_conditioning_cached": attestation[
                "fixed_reference_cached"
            ],
            "sample_rate": 44_100,
            "sdpa_backend": attestation["slow_attention_backend"],
            "fast_attention_backend": attestation["fast_attention_backend"],
            "cuda_graph_requested": attestation["cuda_graph_requested"],
            "cuda_graph_active": attestation["cuda_graph_active"],
            "cuda_graph_batches": attestation["cuda_graph_batches"],
            "codebook_compile_requested": attestation[
                "torch_compile_requested"
            ],
            "codebook_compile_active": attestation["torch_compile_active"],
            "codebook_compile_state": (
                "compiled"
                if attestation["torch_compile_active"]
                else "eager_fallback"
                if attestation["torch_compile_requested"]
                else "eager"
            ),
            "codebook_compile_batches": attestation["torch_compile_batches"],
            "codebook_compile_seconds": None,
            "single_process_attested": attestation[
                "single_process_attested"
            ],
            "backend": "sglang-omni",
            "backend_requests": backend.get("total_requests", 0),
        }

    def normalize_request(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        if FORBIDDEN_REFERENCE_FIELDS.intersection(payload):
            raise ValueError("client-supplied voice references are not supported")
        unknown = set(payload).difference(ALLOWED_FIELDS)
        if unknown:
            raise ValueError("request contains unsupported fields")
        text = payload.get("input")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("input must be a non-empty string")
        text = text.strip()
        if len(text) > self.settings.max_input_characters:
            raise ValueError(
                f"input exceeds {self.settings.max_input_characters} characters"
            )
        if payload.get("model", self.settings.model_name) != self.settings.model_name:
            raise ValueError(f"model must be {self.settings.model_name}")
        if payload.get("response_format", "wav") != "wav":
            raise ValueError("only response_format=wav is supported")
        speed = bounded_number(payload.get("speed"), 1.0, 1.0, 1.0)
        del speed
        normalized = {
            "model": self.settings.model_name,
            "input": text,
            "response_format": "wav",
            "max_new_tokens": bounded_integer(
                payload.get("max_new_tokens"), 512, 32, 1024
            ),
            # SGLang Omni's generic speech layer otherwise applies S2-Pro
            # defaults (0.8/0.8/30), not Audio8's quality defaults.
            "temperature": bounded_number(
                payload.get("temperature"), 0.8, 0.05, 2.0
            ),
            "top_p": bounded_number(payload.get("top_p"), 0.95, 0.05, 1.0),
            "top_k": bounded_integer(payload.get("top_k"), 50, 1, 4096),
        }
        if "seed" in payload:
            normalized["seed"] = bounded_integer(
                payload["seed"], 260_810, 0, 2**31 - 1
            )
        return normalized

    def synthesize_normalized(self, payload: dict[str, Any]) -> tuple[bytes, float]:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        headers, audio, elapsed = self.request_backend(
            "POST",
            self.settings.backend_url,
            headers={
                "Accept": "audio/wav",
                "Content-Type": "application/json",
                "User-Agent": "CerberusAudio8Gateway/1",
            },
            body=encoded,
            maximum_bytes=MAX_WAV_BYTES,
            timeout_seconds=self.settings.timeout_seconds,
        )
        if headers.get_content_type() not in {"audio/wav", "audio/x-wav"}:
            raise RuntimeError("backend returned an invalid content type")
        if len(audio) > MAX_WAV_BYTES:
            raise RuntimeError("backend WAV is too large")
        try:
            with wave.open(io.BytesIO(audio), "rb") as source:
                if (
                    source.getcomptype() != "NONE"
                    or source.getsampwidth() != 2
                    or source.getnchannels() != 1
                    or source.getframerate() != 44_100
                    or source.getnframes() <= 0
                ):
                    raise RuntimeError("backend returned an invalid WAV")
                expected = source.getnframes() * 2
                if len(source.readframes(source.getnframes())) != expected:
                    raise RuntimeError("backend returned a truncated WAV")
        except (EOFError, wave.Error) as error:
            raise RuntimeError("backend returned a malformed WAV") from error
        return audio, elapsed

    def synthesize(self, payload: Any) -> tuple[bytes, float]:
        return self.synthesize_normalized(self.normalize_request(payload))


RUNTIME: GatewayRuntime | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "CerberusAudio8SGLangGateway/1"

    def setup(self) -> None:
        super().setup()
        assert RUNTIME is not None
        self.connection.settimeout(RUNTIME.settings.header_timeout_seconds)
        self._header_timer: threading.Timer | None = None

    def _arm_header_deadline(self) -> None:
        assert RUNTIME is not None

        def expire() -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        self._header_timer = threading.Timer(
            RUNTIME.settings.header_timeout_seconds, expire
        )
        self._header_timer.daemon = True
        self._header_timer.start()

    def _cancel_header_deadline(self) -> None:
        if self._header_timer is not None:
            self._header_timer.cancel()
            self._header_timer = None

    def handle_one_request(self) -> None:
        self._arm_header_deadline()
        try:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
        finally:
            self._cancel_header_deadline()

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            self._cancel_header_deadline()

    def set_write_timeout(self) -> None:
        assert RUNTIME is not None
        self.connection.settimeout(RUNTIME.settings.write_timeout_seconds)

    def write_with_deadline(self, payload: bytes) -> None:
        assert RUNTIME is not None
        deadline = time.monotonic() + RUNTIME.settings.write_timeout_seconds
        view = memoryview(payload)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response write exceeded its total deadline")
            self.connection.settimeout(remaining)
            chunk = view[:65_536]
            self.wfile.write(chunk)
            view = view[len(chunk) :]

    def read_body_with_deadline(self, content_length: int) -> bytes:
        assert RUNTIME is not None
        deadline = time.monotonic() + RUNTIME.settings.body_timeout_seconds
        chunks: list[bytes] = []
        total = 0
        while total < content_length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request body exceeded its total deadline")
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(min(65_536, content_length - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.client_address[0]} {format_string % args}", flush=True)

    def json_response(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.set_write_timeout()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.write_with_deadline(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.json_response(404, {"error": "not found"})
            return
        assert RUNTIME is not None
        status, document = RUNTIME.health_document()
        self.json_response(status, document)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/audio/speech":
            self.json_response(404, {"error": "not found"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            self.json_response(415, {"error": "Content-Type must be application/json"})
            return
        if self.headers.get("Transfer-Encoding"):
            self.json_response(400, {"error": "chunked requests are not supported"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 1 <= content_length <= MAX_HTTP_BODY:
            self.json_response(413, {"error": "invalid request size"})
            return
        assert RUNTIME is not None
        try:
            body = self.read_body_with_deadline(content_length)
        except (TimeoutError, socket.timeout, OSError):
            self.close_connection = True
            self.json_response(408, {"error": "request body timed out"})
            return
        if len(body) != content_length:
            self.close_connection = True
            self.json_response(400, {"error": "request body is truncated"})
            return
        try:
            payload = json.loads(body)
            normalized = RUNTIME.normalize_request(payload)
        except (json.JSONDecodeError, ValueError) as error:
            self.json_response(400, {"error": str(error)})
            return
        if not RUNTIME.request_slots.acquire(blocking=False):
            self.set_write_timeout()
            self.send_response(429)
            encoded = b'{"error":"Audio8 request queue is full; retry later"}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", "5")
            self.end_headers()
            self.write_with_deadline(encoded)
            return
        try:
            try:
                audio, elapsed = RUNTIME.synthesize_normalized(normalized)
            except Exception as error:
                print(f"SGLang synthesis failed: {type(error).__name__}", flush=True)
                self.json_response(502, {"error": "synthesis backend failed"})
                return
        finally:
            RUNTIME.request_slots.release()
        self.set_write_timeout()
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Audio8-Synthetic", "true")
        self.send_header("X-Synthesis-Seconds", f"{elapsed:.3f}")
        self.end_headers()
        self.write_with_deadline(audio)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        max_connections: int,
    ) -> None:
        self.connection_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(server_address, handler)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self.connection_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connection_slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


def main() -> None:
    global RUNTIME
    settings = Settings.from_environment()
    RUNTIME = GatewayRuntime(settings)
    server = BoundedThreadingHTTPServer(
        (settings.listen_host, settings.listen_port),
        Handler,
        max_connections=settings.max_connections,
    )
    print(
        f"Audio8 SGLang gateway listening on {settings.listen_host}:"
        f"{settings.listen_port}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
