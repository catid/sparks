from __future__ import annotations

import contextlib
import dataclasses
import email.message
import http.client
import http.server
import importlib.util
import io
import json
import pathlib
import select
import socket
import sys
import threading
import time
import unittest
import wave
from collections.abc import Iterator
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audio8_sglang_gateway", ROOT / "gateway.py")
assert SPEC and SPEC.loader
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)


def settings() -> gateway.Settings:
    return gateway.Settings(
        backend_url="http://127.0.0.1:18010/v1/audio/speech",
        backend_health_url="http://127.0.0.1:18010/health",
        listen_host="127.0.0.1",
        listen_port=18011,
        model_name="audio8/tts-0.6b",
        max_input_characters=300,
        max_active_requests=2,
        max_connections=4,
        header_timeout_seconds=0.2,
        body_timeout_seconds=0.15,
        write_timeout_seconds=1,
        timeout_seconds=2,
    )


def runtime_attestation(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "schema": 1,
        "runtime_process_id": 41,
        "engine_process_id": 41,
        "reference_process_id": 41,
        "engine_initialized": True,
        "fixed_reference_configured": True,
        "fixed_reference_cached": True,
        "slow_attention_backend": "flashinfer",
        "fast_attention_backend": "flashinfer",
        "cuda_graph_requested": True,
        "cuda_graph_active": True,
        "cuda_graph_batches": [1, 2, 3, 4],
        "torch_compile_requested": True,
        "torch_compile_active": True,
        "torch_compile_batches": [1, 2],
        "single_process_attested": True,
        "ready": True,
    }
    result.update(overrides)
    return result


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(44_100)
        target.writeframes(b"\0\0" * 441)
    return output.getvalue()


def response_headers(content_type: str) -> email.message.Message:
    headers = email.message.Message()
    headers["Content-Type"] = content_type
    return headers


@contextlib.contextmanager
def running_gateway(
    runtime: gateway.GatewayRuntime, *, max_connections: int = 4
) -> Iterator[gateway.BoundedThreadingHTTPServer]:
    previous = gateway.RUNTIME
    gateway.RUNTIME = runtime
    server = gateway.BoundedThreadingHTTPServer(
        ("127.0.0.1", 0), gateway.Handler, max_connections=max_connections
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        gateway.RUNTIME = previous


class SilentHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        return None


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = gateway.GatewayRuntime(settings())

    def test_quality_defaults_are_injected(self) -> None:
        result = self.runtime.normalize_request(
            {
                "model": "audio8/tts-0.6b",
                "input": " Hello. ",
                "response_format": "wav",
                "max_new_tokens": 1024,
            }
        )
        self.assertEqual(result["input"], "Hello.")
        self.assertEqual(result["temperature"], 0.8)
        self.assertEqual(result["top_p"], 0.95)
        self.assertEqual(result["top_k"], 50)
        self.assertNotIn("references", result)

    def test_log_text_escapes_control_characters(self) -> None:
        self.assertEqual(
            gateway.sanitize_log_text("safe\nforged\x1b[31m\x7f"),
            "safe\\x0aforged\\x1b[31m\\x7f",
        )
        self.assertEqual(gateway.sanitize_log_text("plain request"), "plain request")

    def test_client_reference_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "client-supplied"):
            self.runtime.normalize_request(
                {"input": "Hello.", "references": [{"audio_path": "/etc/passwd"}]}
            )

    def test_unknown_fields_and_oversized_token_limit_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.runtime.normalize_request({"input": "Hello.", "extra": True})
        with self.assertRaisesRegex(ValueError, "between 32 and 1024"):
            self.runtime.normalize_request(
                {"input": "Hello.", "max_new_tokens": 1025}
            )

    def test_backend_url_must_be_numeric_loopback(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "numeric loopback"):
            gateway.loopback_http_url(
                "http://localhost:18010/v1/audio/speech", "/v1/audio/speech"
            )
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            gateway.loopback_http_url(
                "http://192.0.2.3:18010/v1/audio/speech", "/v1/audio/speech"
            )

    def test_experimental_ports_and_host_are_immutable(self) -> None:
        unsafe_environments = (
            {"AUDIO8_SGLANG_GATEWAY_HOST": "0.0.0.0"},
            {"AUDIO8_SGLANG_GATEWAY_PORT": "8010"},
            {
                "AUDIO8_SGLANG_BACKEND_URL": (
                    "http://127.0.0.1:8020/v1/audio/speech"
                )
            },
        )
        for environment in unsafe_environments:
            with self.subTest(environment=environment), mock.patch.dict(
                gateway.os.environ, environment, clear=True
            ), self.assertRaisesRegex(RuntimeError, "experimental"):
                gateway.Settings.from_environment()

    def test_production_network_tuple_is_exact_and_explicit(self) -> None:
        with mock.patch.dict(
            gateway.os.environ, {"AUDIO8_SGLANG_PRODUCTION": "1"}, clear=True
        ):
            result = gateway.Settings.from_environment()
        self.assertTrue(result.production)
        self.assertEqual(result.backend_url, gateway.PRODUCTION_BACKEND_URL)
        self.assertEqual(result.listen_host, "0.0.0.0")
        self.assertEqual(result.listen_port, 8010)

        unsafe_environments = (
            {
                "AUDIO8_SGLANG_PRODUCTION": "1",
                "AUDIO8_SGLANG_GATEWAY_HOST": "127.0.0.1",
            },
            {
                "AUDIO8_SGLANG_PRODUCTION": "1",
                "AUDIO8_SGLANG_GATEWAY_PORT": "18011",
            },
            {
                "AUDIO8_SGLANG_PRODUCTION": "1",
                "AUDIO8_SGLANG_BACKEND_URL": gateway.EXPERIMENTAL_BACKEND_URL,
            },
            {
                "AUDIO8_SGLANG_PRODUCTION": "1",
                "AUDIO8_SGLANG_EXPERIMENTAL": "1",
            },
        )
        for environment in unsafe_environments:
            with self.subTest(environment=environment), mock.patch.dict(
                gateway.os.environ, environment, clear=True
            ), self.assertRaises(RuntimeError):
                gateway.Settings.from_environment()

    def test_synthesis_validates_and_returns_pcm_wav(self) -> None:
        audio = wav_bytes()
        backend = mock.Mock(
            return_value=(response_headers("audio/wav"), audio, 0.125)
        )
        with mock.patch.object(self.runtime, "request_backend", backend):
            result, elapsed = self.runtime.synthesize({"input": "Hello."})
        self.assertEqual(result, audio)
        self.assertEqual(elapsed, 0.125)
        self.assertEqual(backend.call_args.args[:2], ("POST", settings().backend_url))
        self.assertNotIn(b"/etc/passwd", backend.call_args.kwargs["body"])

    def test_health_forwards_actual_backend_attestation(self) -> None:
        backend = {
            "status": "healthy",
            "running": True,
            "total_requests": 7,
            "audio8_runtime": runtime_attestation(
                cuda_graph_requested=False,
                cuda_graph_active=False,
                cuda_graph_batches=[],
                torch_compile_requested=True,
                torch_compile_active=False,
                torch_compile_batches=[],
            ),
        }
        request_backend = mock.Mock(
            return_value=(
                response_headers("application/json"),
                json.dumps(backend).encode(),
                0.01,
            )
        )
        with mock.patch.object(
            self.runtime, "request_backend", request_backend
        ):
            status, document = self.runtime.health_document()
        self.assertEqual(status, 200)
        self.assertFalse(document["cuda_graph_active"])
        self.assertEqual(document["cuda_graph_batches"], [])
        self.assertTrue(document["codebook_compile_requested"])
        self.assertFalse(document["codebook_compile_active"])
        self.assertEqual(document["codebook_compile_state"], "eager_fallback")
        self.assertEqual(document["codebook_compile_batches"], [])

    def test_health_fails_closed_without_safe_attestation(self) -> None:
        for backend in (
            {"status": "healthy", "running": True},
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(
                    slow_attention_backend="fa3"
                ),
            },
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(
                    torch_compile_requested=False,
                    torch_compile_active=True,
                ),
            },
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(schema=True),
            },
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(ready=1),
            },
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(engine_process_id=42),
            },
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(
                    cuda_graph_active=False,
                ),
            },
            {
                "status": "healthy",
                "running": True,
                "audio8_runtime": runtime_attestation(
                    torch_compile_batches=[2, 1],
                ),
            },
        ):
            request_backend = mock.Mock(
                return_value=(
                    response_headers("application/json"),
                    json.dumps(backend).encode(),
                    0.01,
                )
            )
            with self.subTest(backend=backend), mock.patch.object(
                self.runtime, "request_backend", request_backend
            ):
                status, document = self.runtime.health_document()
                self.assertEqual(status, 503)
                self.assertEqual(document["status"], "loading")

    def test_production_health_requires_exact_optimized_batches(self) -> None:
        runtime = gateway.GatewayRuntime(
            dataclasses.replace(settings(), production=True)
        )
        backend = {
            "status": "healthy",
            "running": True,
            "audio8_runtime": runtime_attestation(
                cuda_graph_batches=[1, 2, 3, 4],
                torch_compile_batches=[1, 2],
            ),
        }
        request_backend = mock.Mock(
            return_value=(
                response_headers("application/json"),
                json.dumps(backend).encode(),
                0.01,
            )
        )
        with mock.patch.object(runtime, "request_backend", request_backend):
            status, _document = runtime.health_document()
        self.assertEqual(status, 503)

    def test_redirects_are_not_followed(self) -> None:
        sink_hits = 0

        class Sink(SilentHandler):
            def do_GET(self) -> None:  # noqa: N802
                nonlocal sink_hits
                sink_hits += 1
                self.send_response(200)
                self.end_headers()

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        target = f"http://127.0.0.1:{sink.server_port}/target"

        class Redirect(SilentHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()

        redirect = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
        redirect_thread.start()
        try:
            redirect_url = f"http://127.0.0.1:{redirect.server_port}/health"
            runtime = gateway.GatewayRuntime(
                dataclasses.replace(
                    settings(),
                    backend_health_url=redirect_url,
                )
            )
            with self.assertRaisesRegex(RuntimeError, "rejected"):
                runtime.backend_health()
            self.assertEqual(sink_hits, 0)
        finally:
            redirect.shutdown()
            redirect.server_close()
            redirect_thread.join(timeout=2)
            sink.shutdown()
            sink.server_close()
            sink_thread.join(timeout=2)

    def test_backend_trickle_has_total_deadline(self) -> None:
        audio = wav_bytes()

        class Trickle(SilentHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                try:
                    for byte in audio:
                        self.wfile.write(bytes((byte,)))
                        self.wfile.flush()
                        time.sleep(0.03)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        backend = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        runtime = gateway.GatewayRuntime(
            dataclasses.replace(
                settings(),
                backend_url=(
                    f"http://127.0.0.1:{backend.server_port}/v1/audio/speech"
                ),
                timeout_seconds=0.15,
            )
        )
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(TimeoutError, "total deadline"):
                runtime.synthesize({"input": "Hello."})
            self.assertLess(time.monotonic() - started, 0.75)
        finally:
            backend.shutdown()
            backend.server_close()
            thread.join(timeout=2)

    def test_partial_body_times_out_before_synthesis_slot(self) -> None:
        with running_gateway(self.runtime) as server:
            with socket.create_connection(server.server_address, timeout=1) as client:
                client.settimeout(2)
                client.sendall(
                    b"POST /v1/audio/speech HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n{"
                )
                response = client.recv(4096)
            self.assertIn(b" 408 ", response)
            self.assertTrue(self.runtime.request_slots.acquire(blocking=False))
            self.assertTrue(self.runtime.request_slots.acquire(blocking=False))
            self.assertFalse(self.runtime.request_slots.acquire(blocking=False))
            self.runtime.request_slots.release()
            self.runtime.request_slots.release()

    def test_trickled_body_has_total_deadline(self) -> None:
        with running_gateway(self.runtime) as server:
            with socket.create_connection(server.server_address, timeout=1) as client:
                client.settimeout(2)
                client.sendall(
                    b"POST /v1/audio/speech HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 100\r\n\r\n"
                )
                started = time.monotonic()
                response = b""
                while time.monotonic() - started < 1:
                    try:
                        client.sendall(b"{")
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    readable, _, _ = select.select([client], [], [], 0)
                    if readable:
                        response = client.recv(4096)
                        break
                    time.sleep(0.04)
                if not response:
                    response = client.recv(4096)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertIn(b" 408 ", response)

    def test_trickled_headers_have_total_deadline(self) -> None:
        with running_gateway(self.runtime) as server:
            with socket.create_connection(server.server_address, timeout=1) as client:
                client.settimeout(2)
                started = time.monotonic()
                for byte in b"POST /v1/audio/speech HTTP/1.1\r\nHost: 127.0.0.1\r\n":
                    try:
                        client.sendall(bytes((byte,)))
                    except (BrokenPipeError, ConnectionResetError):
                        break
                    time.sleep(0.04)
                try:
                    response = client.recv(4096)
                except ConnectionResetError:
                    response = b""
            self.assertLess(time.monotonic() - started, 0.75)
            self.assertEqual(response, b"")

    def test_full_synthesis_queue_returns_429_after_body_validation(self) -> None:
        self.assertTrue(self.runtime.request_slots.acquire(blocking=False))
        self.assertTrue(self.runtime.request_slots.acquire(blocking=False))
        try:
            with running_gateway(self.runtime) as server:
                connection = http.client.HTTPConnection(*server.server_address, timeout=2)
                body = json.dumps({"input": "Hello."})
                connection.request(
                    "POST",
                    "/v1/audio/speech",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 429)
                self.assertEqual(response.getheader("Retry-After"), "5")
                connection.close()
        finally:
            self.runtime.request_slots.release()
            self.runtime.request_slots.release()

    def test_connection_limit_rejects_excess_connection(self) -> None:
        with running_gateway(self.runtime, max_connections=1) as server:
            self.assertTrue(server.connection_slots.acquire(blocking=False))
            try:
                with socket.create_connection(server.server_address, timeout=1) as client:
                    client.settimeout(1)
                    client.sendall(b"GET /health HTTP/1.0\r\n\r\n")
                    try:
                        result = client.recv(128)
                    except ConnectionResetError:
                        result = b""
                self.assertEqual(result, b"")
            finally:
                server.connection_slots.release()


if __name__ == "__main__":
    unittest.main()
