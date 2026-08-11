from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock


SERVER_PATH = pathlib.Path(__file__).resolve().parents[1] / "server.py"


def load_server_module():
    soundfile = types.ModuleType("soundfile")
    torch = types.ModuleType("torch")
    transformers = types.ModuleType("transformers")
    transformers.AutoModel = object
    transformers.AutoProcessor = object
    spec = importlib.util.spec_from_file_location("audio8_server_tested", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        sys.modules,
        {
            "soundfile": soundfile,
            "torch": torch,
            "transformers": transformers,
        },
    ):
        spec.loader.exec_module(module)
    return module


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_module = load_server_module()

    def test_numeric_defaults_and_invalid_values(self) -> None:
        finite_number = self.server_module.finite_number
        integer = self.server_module.integer

        self.assertEqual(finite_number(None, 0.8, 0.05, 2.0), 0.8)
        for value in (True, "not-a-number", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                finite_number(value, 0.8, 0.05, 2.0)

        self.assertEqual(integer(None, 50, 1, 4096), 50)
        self.assertEqual(integer(50.0, 1, 1, 4096), 50)
        for value in (True, 1.5, "1.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integer(value, 50, 1, 4096)

    def test_every_client_reference_field_is_rejected_even_when_empty(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        for field in (
            "references",
            "reference_audio",
            "reference_audio_path",
            "reference_text",
            "reference_text_file",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "client-supplied voice references"
            ):
                runtime.synthesize({"input": "hello", field: []})


class HttpAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server_module = load_server_module()
        self.server_module.Handler.log_message = lambda *args: None
        self.server_module.REQUEST_SLOTS = threading.BoundedSemaphore(1)
        self.server_module.REQUEST_SLOTS.acquire()
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), self.server_module.Handler
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server_module.REQUEST_SLOTS.release()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def test_full_queue_returns_retryable_429_without_running_model(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.httpd.server_port}/v1/audio/speech",
            data=json.dumps({"input": "hello"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(raised.exception.code, 429)
        self.assertEqual(raised.exception.headers["Retry-After"], "5")


if __name__ == "__main__":
    unittest.main()
