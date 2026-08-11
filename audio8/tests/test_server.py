from __future__ import annotations

import importlib.util
import contextlib
import json
import os
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
    ), mock.patch.dict(os.environ, {"AUDIO8_COMPILE_CODEBOOKS": "0"}):
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

    def test_environment_flag_is_closed(self) -> None:
        environment_flag = self.server_module.environment_flag
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(environment_flag("AUDIO8_TEST_FLAG"))
            self.assertTrue(environment_flag("AUDIO8_TEST_FLAG", True))
        for value, expected in (("0", False), ("1", True)):
            with mock.patch.dict(
                os.environ, {"AUDIO8_TEST_FLAG": value}, clear=True
            ):
                self.assertEqual(environment_flag("AUDIO8_TEST_FLAG"), expected)
        with mock.patch.dict(
            os.environ, {"AUDIO8_TEST_FLAG": "true"}, clear=True
        ), self.assertRaisesRegex(RuntimeError, "must be 0 or 1"):
            environment_flag("AUDIO8_TEST_FLAG")

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

    def test_sdpa_selector_is_closed_and_overrides_only_upstream_choice(self) -> None:
        calls = []

        class Backends:
            EFFICIENT_ATTENTION = "efficient-enum"

        class Context:
            def __enter__(self):
                return None

            def __exit__(self, *_args):
                return None

        def original_kernel(backend):
            calls.append(backend)
            return Context()

        namespace = {
            "sdpa_kernel": original_kernel,
            "SDPBackend": Backends,
        }
        exec("def generate(self):\n    return None\n", namespace)
        model = types.SimpleNamespace(
            generate=types.MethodType(namespace["generate"], object())
        )

        selected = self.server_module.select_sdpa_backend(model, "efficient")
        self.assertEqual(selected, "efficient")
        with namespace["sdpa_kernel"]("ignored"):
            pass
        self.assertEqual(calls, ["efficient-enum"])

        selected = self.server_module.select_sdpa_backend(model, "math")
        self.assertEqual(selected, "math")
        with namespace["sdpa_kernel"]("math-enum"):
            pass
        self.assertEqual(calls[-1], "math-enum")
        with self.assertRaisesRegex(RuntimeError, "must be math or efficient"):
            self.server_module.select_sdpa_backend(model, "flash")

    def test_fixed_reference_uses_cached_codes_not_private_file_per_request(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        runtime.reference_codes = object()
        runtime.reference_text = "private operator transcript"
        runtime.reference_audio = "/private/reference.wav"

        inputs = runtime.processor_inputs("Safe output text")

        self.assertEqual(inputs["text"], ["Safe output text"])
        self.assertIs(inputs["reference_codes"], runtime.reference_codes)
        self.assertEqual(inputs["reference_text"], [runtime.reference_text])
        self.assertNotIn("reference_audio", inputs)

    def test_codebook_compile_prewarms_stock_method_without_changing_settings(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        original = mock.Mock(name="stock_generate_codebooks")
        compiled = mock.Mock(name="compiled_generate_codebooks")
        runtime.model = types.SimpleNamespace(
            _generate_codebooks=original,
            generate=mock.Mock(return_value=object()),
        )
        runtime.processor = mock.Mock(
            return_value={
                "input_ids": types.SimpleNamespace(to=lambda _device: "gpu")
            }
        )
        runtime.device = "cuda:0"
        runtime.reference_codes = None
        runtime.reference_text = None
        runtime.eager_generate_codebooks = original
        runtime.compiled_generate_codebooks = None
        runtime.codebook_compile_active = False
        runtime.codebook_compile_seconds = None
        inference_context = mock.MagicMock()
        inference_context.__enter__.return_value = None
        inference_context.__exit__.return_value = None

        with mock.patch.object(
            runtime, "_verify_executable_compile_cache"
        ), mock.patch.object(
            self.server_module.torch, "compile", return_value=compiled, create=True
        ) as compile_mock, mock.patch.object(
            self.server_module.torch,
            "inference_mode",
            return_value=inference_context,
            create=True,
        ), mock.patch.object(
            self.server_module.torch, "manual_seed", create=True
        ), mock.patch.object(
            self.server_module.torch,
            "cuda",
            types.SimpleNamespace(synchronize=mock.Mock()),
            create=True,
        ), mock.patch("builtins.print"):
            runtime._compile_and_prewarm_codebooks()

        compile_mock.assert_called_once_with(original)
        self.assertIs(runtime.model._generate_codebooks, original)
        self.assertIs(runtime.compiled_generate_codebooks, compiled)
        runtime.model.generate.assert_called_once_with(
            input_ids="gpu",
            max_new_tokens=1,
            temperature=0.8,
            top_p=0.95,
            top_k=50,
            do_sample=True,
            return_dict_in_generate=True,
        )
        self.assertTrue(runtime.codebook_compile_active)
        self.assertIsNotNone(runtime.codebook_compile_seconds)

    def test_codebook_compile_failure_restores_eager_method(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        original = mock.Mock(name="stock_generate_codebooks")
        runtime.model = types.SimpleNamespace(_generate_codebooks=original)
        runtime.eager_generate_codebooks = original
        runtime.compiled_generate_codebooks = None
        runtime.codebook_compile_active = False
        runtime.codebook_compile_seconds = None
        with mock.patch.object(
            runtime,
            "_verify_executable_compile_cache",
            side_effect=RuntimeError("private compiler path"),
        ), mock.patch("builtins.print") as printed:
            runtime._compile_and_prewarm_codebooks()

        self.assertIs(runtime.model._generate_codebooks, original)
        self.assertIsNone(runtime.compiled_generate_codebooks)
        self.assertFalse(runtime.codebook_compile_active)
        self.assertIsNone(runtime.codebook_compile_seconds)
        self.assertNotIn("private compiler path", str(printed.call_args_list))

    def test_compiled_codebooks_are_only_selected_for_production_defaults(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        eager = object()
        compiled = object()
        runtime.eager_generate_codebooks = eager
        runtime.compiled_generate_codebooks = compiled
        runtime.codebook_compile_active = True

        selected, active = runtime.codebook_generator_for(0.8, 0.95, 50)
        self.assertIs(selected, compiled)
        self.assertTrue(active)
        for settings in ((0.7, 0.95, 50), (0.8, 0.9, 50), (0.8, 0.95, 40)):
            with self.subTest(settings=settings):
                selected, active = runtime.codebook_generator_for(*settings)
                self.assertIs(selected, eager)
                self.assertFalse(active)

        runtime.codebook_compile_active = False
        selected, active = runtime.codebook_generator_for(0.8, 0.95, 50)
        self.assertIs(selected, eager)
        self.assertFalse(active)

    def test_queued_request_observes_compiled_failure_eager_fallback(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        eager = object()
        compiled = object()
        first_generate_entered = threading.Event()
        release_first_generate = threading.Event()
        second_inputs_prepared = threading.Event()
        processor_calls = 0

        class Value:
            def to(self, _device):
                return self

            def __getitem__(self, _key):
                return self

            def __int__(self):
                return 1

            def float(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return [0.0]

        def processor(**_kwargs):
            nonlocal processor_calls
            processor_calls += 1
            if processor_calls == 2:
                second_inputs_prepared.set()
            return {"input_ids": Value()}

        def generate(**_kwargs):
            if runtime.model._generate_codebooks is compiled:
                first_generate_entered.set()
                self.assertTrue(release_first_generate.wait(timeout=2))
                raise RuntimeError("compiled graph failed")
            self.assertIs(runtime.model._generate_codebooks, eager)
            return types.SimpleNamespace(codes=Value())

        runtime.device = "cuda:0"
        runtime.processor = processor
        runtime.reference_codes = None
        runtime.reference_text = None
        runtime.model = types.SimpleNamespace(
            _generate_codebooks=eager,
            generate=generate,
            decode_audio=lambda _codes: (Value(), Value()),
        )
        runtime.eager_generate_codebooks = eager
        runtime.compiled_generate_codebooks = compiled
        runtime.codebook_compile_active = True
        runtime.sample_rate = 44100
        errors = []
        results = []

        def invoke():
            try:
                results.append(runtime.synthesize({"input": "hello"}))
            except Exception as error:  # The first request is expected to fail.
                errors.append(error)

        lock = threading.Lock()
        first = threading.Thread(target=invoke)
        second = threading.Thread(target=invoke)
        with mock.patch.object(
            self.server_module, "INFERENCE_LOCK", lock
        ), mock.patch.object(
            self.server_module.torch,
            "inference_mode",
            return_value=contextlib.nullcontext(),
            create=True,
        ), mock.patch.object(
            self.server_module.torch, "manual_seed", create=True
        ), mock.patch.object(self.server_module.sf, "write", create=True):
            first.start()
            self.assertTrue(first_generate_entered.wait(timeout=2))
            second.start()
            self.assertTrue(second_inputs_prepared.wait(timeout=2))
            release_first_generate.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "compiled graph failed")
        self.assertEqual(len(results), 1)
        self.assertFalse(runtime.codebook_compile_active)
        self.assertIsNone(runtime.compiled_generate_codebooks)
        self.assertIs(runtime.model._generate_codebooks, eager)

    def test_successful_compiled_request_restores_eager_method_at_rest(self) -> None:
        runtime = object.__new__(self.server_module.Audio8Runtime)
        eager = object()
        compiled = object()

        class Value:
            def to(self, _device):
                return self

            def __getitem__(self, _key):
                return self

            def __int__(self):
                return 1

            def float(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return [0.0]

        def generate(**_kwargs):
            self.assertIs(runtime.model._generate_codebooks, compiled)
            return types.SimpleNamespace(codes=Value())

        runtime.device = "cuda:0"
        runtime.processor = lambda **_kwargs: {"input_ids": Value()}
        runtime.reference_codes = None
        runtime.reference_text = None
        runtime.model = types.SimpleNamespace(
            _generate_codebooks=eager,
            generate=generate,
            decode_audio=lambda _codes: (Value(), Value()),
        )
        runtime.eager_generate_codebooks = eager
        runtime.compiled_generate_codebooks = compiled
        runtime.codebook_compile_active = True
        runtime.sample_rate = 44100

        with mock.patch.object(
            self.server_module.torch,
            "inference_mode",
            return_value=contextlib.nullcontext(),
            create=True,
        ), mock.patch.object(
            self.server_module.torch, "manual_seed", create=True
        ), mock.patch.object(self.server_module.sf, "write", create=True):
            runtime.synthesize({"input": "hello"})

        self.assertTrue(runtime.codebook_compile_active)
        self.assertIs(runtime.compiled_generate_codebooks, compiled)
        self.assertIs(runtime.model._generate_codebooks, eager)


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
        self.server_module.RUNTIME = None
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

    def test_health_reports_compiled_or_eager_fallback_state(self) -> None:
        self.server_module.RUNTIME = types.SimpleNamespace(
            reference_audio="/private/reference.wav",
            reference_codes=object(),
            sample_rate=44100,
            sdpa_backend="efficient",
            codebook_compile_requested=True,
            codebook_compile_active=False,
            codebook_compile_seconds=None,
        )
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.httpd.server_port}/health", timeout=2
        ) as response:
            payload = json.load(response)

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["codebook_compile_state"], "eager_fallback")
        self.assertTrue(payload["codebook_compile_requested"])
        self.assertFalse(payload["codebook_compile_active"])
        self.assertNotIn("/private", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
