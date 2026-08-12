from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import struct
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


BRIDGE_PATH = pathlib.Path(__file__).resolve().parents[1] / "voice_bridge.py"


def load_bridge_module():
    spec = importlib.util.spec_from_file_location("voice_bridge_tested", BRIDGE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


class BridgeTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_bridge_module()

    def settings(self, **overrides):
        values = {
            "asr_url": "http://127.0.0.1:8020/transcribe",
            "openclaw_url": "http://127.0.0.1:18789/v1/chat/completions",
            "openclaw_token": "",
            "openclaw_model": "openclaw",
            "openclaw_user": "cerberus3-voice",
            "tts_url": "http://127.0.0.1:8010/v1/audio/speech",
            "tts_model": "audio8/tts-0.6b",
            "capture_device": "plughw:CARD=CP900,DEV=0",
            "playback_device": "plughw:CARD=CP900,DEV=0",
            "frame_ms": 20,
            "pre_roll_ms": 300,
            "speech_start_ms": 80,
            "trailing_silence_ms": 700,
            "minimum_voice_ms": 180,
            "maximum_utterance_seconds": 30,
            "vad_minimum_rms": 350,
            "vad_noise_ratio": 3.0,
            "armed_seconds": 12,
            "playback_cooldown_seconds": 1,
            "asr_timeout_seconds": 120,
            "openclaw_timeout_seconds": 900,
            "tts_timeout_seconds": 240,
            "log_transcripts": False,
            "state_dir": None,
        }
        values.update(overrides)
        return self.module.Settings(**values)

    class ImmediatePlayback:
        def __init__(self) -> None:
            self.cancelled = False
            self.waited = False

        def wait(self, stop_event) -> bool:
            self.waited = True
            return not stop_event.is_set()

        def poll(self) -> bool:
            self.waited = True
            return True

        def cancel(self) -> None:
            self.cancelled = True

    def immediate_playback(self, *_args, **_kwargs):
        return self.ImmediatePlayback()

    def inline_synthesis_worker_class(self):
        tested_module = self.module

        class InlineSynthesisWorker:
            def __init__(self, settings):
                self.settings = settings
                self.result = None
                self.closed = False
                self.busy = False

            def submit(self, text):
                self.result = tested_module.synthesize(self.settings, text)
                self.busy = True

            def poll(self):
                result = self.result
                self.result = None
                self.busy = False
                return result

            def cancel(self):
                self.closed = True
                self.busy = False

            def close(self):
                self.closed = True
                self.busy = False

        return InlineSynthesisWorker


class WakeWordTests(BridgeTestCase):
    def test_wake_word_must_be_one_of_first_two_words(self) -> None:
        router = self.module.WakeWordRouter(armed_seconds=12)
        self.assertEqual(
            router.route("Hey Cerberus, give me the cluster status", now=10),
            ("give me the cluster status", "command"),
        )
        self.assertEqual(
            router.route("Please ask Cerberus for status", now=20),
            (None, "ignored"),
        )
        self.assertEqual(
            router.route("Cerberus: summarize the latest run", now=30),
            ("summarize the latest run", "command"),
        )

    def test_historical_asr_misspelling_remains_input_only_alias(self) -> None:
        router = self.module.WakeWordRouter(armed_seconds=12)
        historical_alias = "Cere" + "brus"
        self.assertEqual(
            router.route(f"{historical_alias}, report status", now=40),
            ("report status", "command"),
        )

    def test_wake_only_arms_exactly_one_following_utterance(self) -> None:
        router = self.module.WakeWordRouter(armed_seconds=12)
        self.assertEqual(router.route("Cerberus", now=100), (None, "armed"))
        self.assertEqual(
            router.route("What is using the GPU?", now=105),
            ("What is using the GPU?", "command"),
        )
        self.assertEqual(router.route("And the CPU?", now=106), (None, "ignored"))

    def test_expired_arm_requires_another_wake_word(self) -> None:
        router = self.module.WakeWordRouter(armed_seconds=5)
        router.route("Cerberus", now=10)
        self.assertEqual(router.route("Status please", now=16), (None, "ignored"))


class VadTests(BridgeTestCase):
    @staticmethod
    def frame(amplitude: int, milliseconds: int = 20) -> bytes:
        return struct.pack("<h", amplitude) * (16_000 * milliseconds // 1000)

    def test_energy_vad_keeps_pre_roll_and_trailing_silence(self) -> None:
        vad = self.module.EnergyVad(self.settings())
        result = None
        for _ in range(15):
            result = vad.feed(self.frame(20))
        for _ in range(10):
            result = vad.feed(self.frame(4_000))
        for _ in range(35):
            result = vad.feed(self.frame(20))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertGreaterEqual(len(result), 45 * len(self.frame(20)))
        self.assertIn(self.frame(4_000), result)

    def test_short_impulse_is_not_an_utterance(self) -> None:
        vad = self.module.EnergyVad(self.settings())
        results = []
        for amplitude in [20] * 15 + [4_000] * 4 + [20] * 35:
            results.append(vad.feed(self.frame(amplitude)))
        self.assertTrue(all(result is None for result in results))

    def test_capture_restores_status_after_rejected_burst_then_continues(self) -> None:
        class FakeVad:
            def __init__(self, _settings):
                self.active_frames = None
                self.calls = 0

            def feed(self, frame):
                self.calls += 1
                if self.calls == 1:
                    self.active_frames = [frame]
                    return None
                if self.calls == 2:
                    self.active_frames = None
                    return None
                if self.calls == 3:
                    self.active_frames = [frame]
                    return None
                self.active_frames = None
                return b"accepted-pcm"

        class FakeRecorder:
            def __init__(self, _settings):
                self.stopped = False

            def read_frame(self):
                return b"frame"

            def stop(self):
                self.stopped = True

        bridge = self.module.VoiceBridge(self.settings())
        bridge.status = mock.Mock(spec=self.module.StatusPublisher)
        with (
            mock.patch.object(self.module, "EnergyVad", FakeVad),
            mock.patch.object(self.module, "Recorder", FakeRecorder),
            mock.patch.object(bridge, "handle_utterance", return_value=True) as handle,
        ):
            self.assertTrue(bridge.capture_one())

        self.assertEqual(bridge.status.speech_detected.call_count, 2)
        bridge.status.resume_listening.assert_called_once_with()
        handle.assert_called_once_with(b"accepted-pcm")


class ResponseTests(BridgeTestCase):
    def test_chunks_are_never_over_audio8_limit(self) -> None:
        text = (
            "The cluster is healthy. "
            + "This deliberately long sentence contains several useful observations " * 8
            + "Done."
        )
        chunks = self.module.chunk_for_tts(text, 140)
        self.assertTrue(chunks)
        self.assertTrue(all(1 <= len(chunk) <= 140 for chunk in chunks))
        self.assertEqual(" ".join(chunks), " ".join(text.split()))

    def test_spoken_response_has_immutable_character_and_chunk_caps(self) -> None:
        private_tail = "DO NOT SYNTHESIZE THIS PRIVATE TAIL"
        answer = ("lengthy model output. " * 1_000) + private_tail
        chunks, truncated = self.module.bounded_spoken_chunks(answer)
        self.assertTrue(truncated)
        self.assertLessEqual(len(chunks), self.module.MAX_TTS_CHUNKS)
        self.assertLessEqual(
            sum(len(chunk) for chunk in chunks),
            self.module.MAX_SPOKEN_CHARACTERS,
        )
        self.assertTrue(
            all(
                len(chunk) <= self.module.TTS_CHUNK_CHARACTERS
                for chunk in chunks
            )
        )
        self.assertNotIn(private_tail, " ".join(chunks))

    def test_local_http_opener_explicitly_has_no_proxies(self) -> None:
        # Passing this empty handler also suppresses build_opener's default,
        # environment-derived ProxyHandler.
        self.assertIsInstance(
            self.module._EMPTY_PROXY_HANDLER,
            self.module.urllib.request.ProxyHandler,
        )
        self.assertEqual(self.module._EMPTY_PROXY_HANDLER.proxies, {})
        self.assertTrue(
            any(
                isinstance(handler, self.module._NoRedirectHandler)
                for handler in self.module._LOCAL_HTTP_OPENER.handlers
            )
        )

    def test_extracts_string_and_structured_final_content(self) -> None:
        self.assertEqual(
            self.module.extract_final_text(
                {"choices": [{"message": {"content": "Final answer"}}]}
            ),
            "Final answer",
        )
        self.assertEqual(
            self.module.extract_final_text(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "Part one"},
                                    {"type": "output_text", "text": "Part two"},
                                    {"type": "reasoning", "text": "private"},
                                ]
                            }
                        }
                    ]
                }
            ),
            "Part one\nPart two",
        )

    def test_remote_dependency_urls_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            self.module.require_loopback_http_url(
                "TEST_URL", "https://example.com/v1/chat/completions"
            )
        with self.assertRaisesRegex(RuntimeError, "loopback"):
            self.module.require_loopback_http_url(
                "TEST_URL", "http://192.0.2.28:8889/v1/chat/completions"
            )

    def test_default_logging_does_not_emit_transcript_or_command(self) -> None:
        settings = self.settings()
        bridge = self.module.VoiceBridge(settings)
        private_transcript = "Cerberus reveal the private spoken request"
        private_answer = "This is a private synthesized answer."
        output = io.StringIO()
        with (
            mock.patch.object(self.module, "transcribe_wav", return_value=private_transcript),
            mock.patch.object(self.module, "ask_openclaw", return_value=private_answer),
            mock.patch.object(self.module, "synthesize", return_value=b"wav"),
            mock.patch.object(
                self.module,
                "start_playback",
                side_effect=self.immediate_playback,
            ),
            mock.patch.object(
                self.module,
                "SynthesisWorker",
                self.inline_synthesis_worker_class(),
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertTrue(bridge.handle_utterance(b"pcm"))
        logged = output.getvalue()
        self.assertNotIn(private_transcript, logged)
        self.assertNotIn(private_answer, logged)
        self.assertIn("content logging disabled", logged)

    def test_truncation_is_logged_without_leaking_response_content(self) -> None:
        settings = self.settings()
        bridge = self.module.VoiceBridge(settings)
        private_answer = "private-model-output " * 1_000
        synthesized_chunks = []
        output = io.StringIO()

        def fake_synthesize(_settings, chunk):
            synthesized_chunks.append(chunk)
            return b"wav"

        with (
            mock.patch.object(
                self.module,
                "transcribe_wav",
                return_value="Cerberus answer briefly",
            ),
            mock.patch.object(self.module, "ask_openclaw", return_value=private_answer),
            mock.patch.object(self.module, "synthesize", side_effect=fake_synthesize),
            mock.patch.object(
                self.module,
                "start_playback",
                side_effect=self.immediate_playback,
            ),
            mock.patch.object(
                self.module,
                "SynthesisWorker",
                self.inline_synthesis_worker_class(),
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertTrue(bridge.handle_utterance(b"pcm"))

        self.assertLessEqual(len(synthesized_chunks), self.module.MAX_TTS_CHUNKS)
        logged = output.getvalue()
        self.assertIn("response truncated for speech", logged)
        self.assertNotIn("private-model-output", logged)


class HttpDeadlineAndRetryTests(BridgeTestCase):
    def serve(self, handler_class):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_total_deadline_stops_a_trickled_json_response(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                for _ in range(100):
                    try:
                        self.wfile.write(b" ")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.04)

        server = self.serve(Handler)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.module.post_json(
                f"http://127.0.0.1:{server.server_port}/slow",
                {},
                timeout=0.15,
            )
        self.assertLess(time.monotonic() - started, 0.5)

    def test_cancel_event_aborts_a_blocked_response_read(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.flush()
                time.sleep(1)

        server = self.serve(Handler)
        cancelled = threading.Event()
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(InterruptedError):
                self.module.post_json(
                    f"http://127.0.0.1:{server.server_port}/blocked",
                    {},
                    timeout=5,
                    cancel_event=cancelled,
                )
        finally:
            timer.cancel()
        self.assertLess(time.monotonic() - started, 0.6)

    def test_tts_retries_429_and_503_with_capped_retry_after(self) -> None:
        wav = self.module.fallback_thinking_cue()

        class Handler(BaseHTTPRequestHandler):
            attempts = 0

            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                type(self).attempts += 1
                if type(self).attempts <= 2:
                    self.send_response(429 if type(self).attempts == 1 else 503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", "2")
                    self.send_header("Retry-After", "999")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav)))
                self.end_headers()
                self.wfile.write(wav)

        server = self.serve(Handler)
        settings = self.settings(
            tts_url=f"http://127.0.0.1:{server.server_port}/v1/audio/speech"
        )
        with mock.patch.object(self.module.time, "sleep") as sleep:
            self.assertEqual(self.module.synthesize(settings, "hello"), wav)
        self.assertEqual(Handler.attempts, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2.0), mock.call(2.0)])

    def test_tts_does_not_retry_a_non_transient_status(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            attempts = 0

            def log_message(self, *_args):
                pass

            def do_POST(self):  # noqa: N802
                type(self).attempts += 1
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        server = self.serve(Handler)
        settings = self.settings(
            tts_url=f"http://127.0.0.1:{server.server_port}/v1/audio/speech"
        )
        with self.assertRaises(self.module.LocalHttpStatusError) as raised:
            self.module.synthesize(settings, "hello")
        self.assertEqual(raised.exception.code, 500)
        self.assertEqual(Handler.attempts, 1)

    def test_retry_after_http_date_and_invalid_values_are_bounded(self) -> None:
        future = self.module.email.utils.format_datetime(
            self.module.datetime.now(self.module.timezone.utc)
            + self.module.datetime.resolution * 10_000_000
        )
        self.assertEqual(
            self.module.retry_after_delay(future),
            self.module.TTS_RETRY_MAX_DELAY_SECONDS,
        )
        self.assertEqual(self.module.retry_after_delay("not a date"), 0.25)

    def test_dependency_probe_requires_semantic_readiness(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            ready = False

            def log_message(self, *_args):
                pass

            def do_GET(self):  # noqa: N802
                payload = json.dumps({"ready": type(self).ready}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = self.serve(Handler)
        settings = self.settings(
            openclaw_url=(
                f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
            )
        )
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            self.module.probe_dependency(settings, "openclaw")
        Handler.ready = True
        self.module.probe_dependency(settings, "openclaw")


class PlaybackPrimitiveTests(BridgeTestCase):
    class FakeProcess:
        def __init__(self, return_code=0):
            self.return_code = return_code
            self.terminated = False
            self.killed = False

        def poll(self):
            return self.return_code

        def terminate(self):
            self.terminated = True
            self.return_code = -15

        def kill(self):
            self.killed = True
            self.return_code = -9

        def wait(self, timeout=None):
            del timeout
            return self.return_code

    def test_start_playback_uses_exact_ram_wav_and_closes_memfd(self) -> None:
        payload = self.module.fallback_thinking_cue()
        captured = {}
        process = self.FakeProcess()

        def fake_popen(argv, *, stdin, stdout, stderr):
            captured["argv"] = argv
            captured["fd"] = stdin
            captured["stdout"] = stdout
            captured["stderr"] = stderr
            self.module.os.lseek(stdin, 0, self.module.os.SEEK_SET)
            captured["payload"] = self.module.os.read(stdin, len(payload) + 1)
            return process

        with mock.patch.object(self.module.subprocess, "Popen", side_effect=fake_popen):
            playback = self.module.start_playback(self.settings(), payload)

        self.assertIs(playback.process, process)
        self.assertEqual(captured["payload"], payload)
        self.assertEqual(captured["argv"][0], "/usr/bin/aplay")
        self.assertNotIn("/tmp", " ".join(captured["argv"]))
        with self.assertRaises(OSError):
            self.module.os.fstat(captured["fd"])

    def test_start_playback_closes_memfd_when_aplay_cannot_start(self) -> None:
        payload = self.module.fallback_thinking_cue()
        captured = {}

        def failed_popen(_argv, *, stdin, **_kwargs):
            captured["fd"] = stdin
            raise OSError("aplay unavailable")

        with (
            mock.patch.object(
                self.module.subprocess, "Popen", side_effect=failed_popen
            ),
            self.assertRaises(OSError),
        ):
            self.module.start_playback(self.settings(), payload)
        with self.assertRaises(OSError):
            self.module.os.fstat(captured["fd"])

    def test_playback_exit_timeout_and_stop_are_bounded(self) -> None:
        stopped = self.module.threading.Event()
        failed = self.FakeProcess(return_code=7)
        with self.assertRaisesRegex(RuntimeError, "status 7"):
            self.module.PlaybackProcess(failed, 10).wait(stopped)

        timed_out = self.FakeProcess(return_code=None)
        timeout_playback = self.module.PlaybackProcess(timed_out, 10)
        timeout_playback.deadline = self.module.time.monotonic() - 1
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            timeout_playback.wait(stopped)
        self.assertTrue(timed_out.terminated)

        cancelled = self.FakeProcess(return_code=None)
        stopped.set()
        self.assertFalse(
            self.module.PlaybackProcess(cancelled, 10).wait(stopped)
        )
        self.assertTrue(cancelled.terminated)

    def test_stop_during_playback_spawn_cancels_the_new_child(self) -> None:
        bridge = self.module.VoiceBridge(self.settings())
        playback = self.ImmediatePlayback()

        def spawn_then_signal(*_args, **_kwargs):
            bridge.request_stop()
            return playback

        with (
            mock.patch.object(
                self.module, "start_playback", side_effect=spawn_then_signal
            ),
            self.assertRaisesRegex(RuntimeError, "stopping"),
        ):
            bridge._start_playback(self.module.fallback_thinking_cue())

        self.assertTrue(playback.cancelled)
        self.assertIsNone(bridge._active_playback)

    def test_synthesis_child_receives_tts_only_configuration(self) -> None:
        captured = {}

        class FakeConnection:
            def send(self, _value):
                pass

            def close(self):
                pass

        class FakeProcess:
            def __init__(self, *, target, args, name, daemon):
                captured.update(
                    target=target,
                    args=args,
                    name=name,
                    daemon=daemon,
                )
                self.alive = True

            def start(self):
                pass

            def is_alive(self):
                return self.alive

            def join(self, timeout=None):
                del timeout
                self.alive = False

            def terminate(self):
                self.alive = False

            def kill(self):
                self.alive = False

        class FakeContext:
            def Pipe(self, duplex):
                self_test.assertTrue(duplex)
                return FakeConnection(), FakeConnection()

            def Process(self, **kwargs):
                return FakeProcess(**kwargs)

        self_test = self
        settings = self.settings(openclaw_token="private-token")
        with mock.patch.object(
            self.module.multiprocessing,
            "get_context",
            return_value=FakeContext(),
        ):
            worker = self.module.SynthesisWorker(settings)
            worker.close()

        child_settings = captured["args"][0]
        self.assertIsInstance(child_settings, self.module.TtsClientSettings)
        self.assertEqual(
            set(vars(child_settings)),
            {"tts_url", "tts_model", "tts_timeout_seconds"},
        )
        self.assertNotIn("private-token", repr(captured["args"]))
        self.assertEqual(captured["name"], "audio8-client")
        self.assertTrue(captured["daemon"])

    def test_synthesis_worker_success_safe_error_and_timeout(self) -> None:
        payload = self.module.fallback_thinking_cue()

        class FakeProcess:
            def __init__(self):
                self.alive = True
                self.terminated = False

            def is_alive(self):
                return self.alive

            def terminate(self):
                self.terminated = True
                self.alive = False

            def kill(self):
                self.alive = False

            def join(self, timeout=None):
                del timeout

        class FakeConnection:
            def __init__(self, result=None, ready=True):
                self.result = result
                self.ready = ready
                self.closed = False

            def poll(self, timeout):
                self_test.assertEqual(timeout, 0)
                return self.ready

            def recv(self):
                return self.result

            def close(self):
                self.closed = True

        self_test = self

        def worker_with(connection):
            worker = object.__new__(self.module.SynthesisWorker)
            worker.connection = connection
            worker.process = FakeProcess()
            worker.timeout_seconds = 10
            worker.deadline = self.module.time.monotonic() + 10
            worker.busy = True
            worker.closed = False
            return worker

        success = worker_with(FakeConnection(("ok", payload)))
        self.assertEqual(success.poll(), payload)
        self.assertFalse(success.busy)

        safe_error = worker_with(
            FakeConnection(("error", "TimeoutError PRIVATE BODY MUST NOT PASS"))
        )
        with self.assertRaises(self.module.SynthesisWorkerError) as caught:
            safe_error.poll()
        self.assertNotIn("PRIVATE BODY", repr(caught.exception))

        timeout_connection = FakeConnection(ready=False)
        timed_out = worker_with(timeout_connection)
        timed_out.deadline = self.module.time.monotonic() - 1
        with self.assertRaisesRegex(
            self.module.SynthesisWorkerError, "timed out"
        ):
            timed_out.poll()
        self.assertTrue(timed_out.process.terminated)
        self.assertTrue(timeout_connection.closed)

    def test_real_spawned_worker_returns_a_safe_loopback_error(self) -> None:
        # Use the canonical import name so multiprocessing's spawn method can
        # re-import the target in its clean child interpreter.
        canonical = importlib.import_module("voice_assistant.voice_bridge")
        config = canonical.TtsClientSettings(
            tts_url="http://127.0.0.1:1/v1/audio/speech",
            tts_model="test-model",
            tts_timeout_seconds=10,
        )
        worker = canonical.SynthesisWorker(config)
        try:
            worker.submit("bounded process smoke test")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    result = worker.poll()
                except canonical.SynthesisWorkerError as error:
                    self.assertNotIn("bounded process smoke test", str(error))
                    break
                self.assertIsNone(result)
                time.sleep(0.02)
            else:
                self.fail("spawned Audio8 client did not return within five seconds")
        finally:
            worker.close()


class PlaybackPipelineTests(BridgeTestCase):
    def run_command(self, bridge, patches):
        defaults = {
            "transcribe_wav": mock.Mock(return_value="Cerberus run the task"),
            "ask_openclaw": mock.Mock(return_value="answer"),
            "SynthesisWorker": self.inline_synthesis_worker_class(),
        }
        defaults.update(patches)
        patchers = [
            mock.patch.object(self.module, name, replacement)
            for name, replacement in defaults.items()
        ]
        for patcher in patchers:
            patcher.start()
        try:
            return bridge.handle_utterance(b"pcm")
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

    def test_response_pipeline_synthesizes_exactly_one_chunk_ahead(self) -> None:
        bridge = self.module.VoiceBridge(self.settings())
        events = []
        active = {"chunk": None}

        class FakePlayback:
            def __init__(self, chunk):
                self.chunk = chunk

            def wait(self, stop_event):
                self_test.assertFalse(stop_event.is_set())
                self_test.assertEqual(active["chunk"], self.chunk)
                events.append(("wait", self.chunk))
                active["chunk"] = None
                return True

            def poll(self):
                self_test.assertEqual(active["chunk"], self.chunk)
                events.append(("wait", self.chunk))
                active["chunk"] = None
                return True

            def cancel(self):
                events.append(("cancel", self.chunk))

        self_test = self

        def fake_synthesize(_settings, text, **_kwargs):
            chunk = int(text[-1])
            if chunk > 1:
                self.assertEqual(active["chunk"], chunk - 1)
            events.append(("synthesize", chunk))
            return f"wav-{chunk}".encode()

        def fake_start(_settings, wav_bytes, **_kwargs):
            chunk = int(wav_bytes.decode().split("-")[1])
            self.assertIsNone(active["chunk"])
            active["chunk"] = chunk
            events.append(("start", chunk))
            return FakePlayback(chunk)

        with (
            mock.patch.object(bridge, "_start_thinking_cue", return_value=None),
            mock.patch.object(
                self.module,
                "bounded_spoken_chunks",
                return_value=(["chunk1", "chunk2", "chunk3"], False),
            ),
        ):
            played = self.run_command(
                bridge,
                {
                    "synthesize": mock.Mock(side_effect=fake_synthesize),
                    "start_playback": mock.Mock(side_effect=fake_start),
                },
            )

        self.assertTrue(played)
        self.assertIsNone(active["chunk"])
        self.assertEqual(
            events,
            [
                ("synthesize", 1),
                ("start", 1),
                ("synthesize", 2),
                ("wait", 1),
                ("start", 2),
                ("synthesize", 3),
                ("wait", 2),
                ("start", 3),
                ("wait", 3),
            ],
        )

    def test_coordinator_interleaves_async_synthesis_and_playback_polls(self) -> None:
        bridge = self.module.VoiceBridge(self.settings())
        events = []

        class AsyncWorker:
            def __init__(self, _settings):
                self.closed = False
                self.text = None
                self.poll_count = 0
                self.busy = False

            def submit(self, text):
                self.text = text
                self.poll_count = 0
                self.busy = True
                events.append(f"submit-{text}")

            def poll(self):
                self.poll_count += 1
                events.append(f"synth-poll-{self.text}-{self.poll_count}")
                if self.poll_count < 3:
                    return None
                self.busy = False
                return f"wav-{self.text}".encode()

            def cancel(self):
                self.closed = True
                self.busy = False

            def close(self):
                self.closed = True
                self.busy = False

        class FirstPlayback:
            def __init__(self):
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                events.append(f"play-poll-{self.poll_count}")
                return self.poll_count >= 2

            def wait(self, _stop_event):
                raise AssertionError("overlapped chunk must be polled")

            def cancel(self):
                events.append("play-cancel")

        def fake_start(_settings, wav_bytes, **_kwargs):
            if wav_bytes == b"wav-first":
                events.append("play-start-first")
                return FirstPlayback()
            events.append("play-start-second")
            return self.ImmediatePlayback()

        with (
            mock.patch.object(bridge, "_start_thinking_cue", return_value=None),
            mock.patch.object(
                self.module,
                "bounded_spoken_chunks",
                return_value=(["first", "second"], False),
            ),
        ):
            self.assertTrue(
                self.run_command(
                    bridge,
                    {
                        "SynthesisWorker": AsyncWorker,
                        "start_playback": mock.Mock(side_effect=fake_start),
                    },
                )
            )

        first_start = events.index("play-start-first")
        second_submit = events.index("submit-second")
        second_start = events.index("play-start-second")
        self.assertLess(first_start, second_submit)
        self.assertEqual(
            events[second_submit + 1 : second_start],
            [
                "synth-poll-second-1",
                "play-poll-1",
                "synth-poll-second-2",
                "play-poll-2",
                "synth-poll-second-3",
            ],
        )

    def test_prefetch_failure_finishes_current_audio_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
            bridge.status.start()
            events = []

            class FakePlayback:
                def wait(self, _stop_event):
                    events.append("wait-current")
                    return True

                def poll(self):
                    events.append("wait-current")
                    return True

                def cancel(self):
                    events.append("cancel-current")

            def fake_synthesize(_settings, text, **_kwargs):
                events.append(f"synthesize-{text}")
                if text == "second":
                    raise TimeoutError("private next chunk")
                return b"first-wav"

            def fake_start(_settings, wav_bytes, **_kwargs):
                events.append(f"start-{wav_bytes.decode()}")
                return FakePlayback()

            with (
                mock.patch.object(bridge, "_start_thinking_cue", return_value=None),
                mock.patch.object(
                    self.module,
                    "bounded_spoken_chunks",
                    return_value=(["first", "second"], False),
                ),
                self.assertRaises(TimeoutError),
            ):
                self.run_command(
                    bridge,
                    {
                        "synthesize": mock.Mock(side_effect=fake_synthesize),
                        "start_playback": mock.Mock(side_effect=fake_start),
                    },
                )
            status, _ = StatusPublisherTests.read_status(directory)
            bridge.status.stop()

        self.assertEqual(
            events,
            [
                "synthesize-first",
                "start-first-wav",
                "synthesize-second",
                "wait-current",
            ],
        )
        self.assertEqual(status["last_error"]["stage"], "tts_synthesis")
        self.assertEqual(status["pipeline"]["steps"]["tts"], "error")
        self.assertEqual(status["pipeline"]["steps"]["play"], "complete")

    def test_playback_failure_discards_the_single_prefetched_chunk(self) -> None:
        bridge = self.module.VoiceBridge(self.settings())
        events = []

        class FailedPlayback:
            def wait(self, _stop_event):
                events.append("wait-first")
                raise OSError("private playback detail")

            def poll(self):
                events.append("wait-first")
                raise OSError("private playback detail")

            def cancel(self):
                events.append("cancel-first")

        def fake_synthesize(_settings, text, **_kwargs):
            events.append(f"synthesize-{text}")
            return text.encode()

        def fake_start(_settings, wav_bytes, **_kwargs):
            events.append(f"start-{wav_bytes.decode()}")
            return FailedPlayback()

        with (
            mock.patch.object(bridge, "_start_thinking_cue", return_value=None),
            mock.patch.object(
                self.module,
                "bounded_spoken_chunks",
                return_value=(["first", "second"], False),
            ),
            self.assertRaises(OSError),
        ):
            self.run_command(
                bridge,
                {
                    "synthesize": mock.Mock(side_effect=fake_synthesize),
                    "start_playback": mock.Mock(side_effect=fake_start),
                },
            )

        self.assertEqual(
            events,
            [
                "synthesize-first",
                "start-first",
                "synthesize-second",
                "wait-first",
            ],
        )

    def test_stop_cancels_active_playback_and_never_starts_prefetched_audio(self) -> None:
        bridge = self.module.VoiceBridge(self.settings())
        events = []

        class CancelledPlayback:
            def wait(self, stop_event):
                events.append("wait-first")
                self_test.assertTrue(stop_event.is_set())
                return False

            def poll(self):
                events.append("wait-first")
                return False

            def cancel(self):
                events.append("cancel-first")

        self_test = self

        def fake_synthesize(_settings, text, **_kwargs):
            events.append(f"synthesize-{text}")
            if text == "second":
                bridge.request_stop()
            return text.encode()

        def fake_start(_settings, wav_bytes, **_kwargs):
            events.append(f"start-{wav_bytes.decode()}")
            return CancelledPlayback()

        with (
            mock.patch.object(bridge, "_start_thinking_cue", return_value=None),
            mock.patch.object(
                self.module,
                "bounded_spoken_chunks",
                return_value=(["first", "second"], False),
            ),
        ):
            played = self.run_command(
                bridge,
                {
                    "synthesize": mock.Mock(side_effect=fake_synthesize),
                    "start_playback": mock.Mock(side_effect=fake_start),
                },
            )

        self.assertFalse(played)
        self.assertIsNone(bridge._active_playback)
        self.assertEqual(
            events,
            [
                "synthesize-first",
                "start-first",
                "synthesize-second",
                "cancel-first",
            ],
        )

    def test_thinking_cue_overlaps_claw_but_not_answer_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
            bridge.status.start()
            events = []

            class CuePlayback:
                def wait(self, _stop_event):
                    events.append("cue-wait")
                    return True

                def cancel(self):
                    events.append("cue-cancel")

            class AnswerPlayback:
                def wait(self, _stop_event):
                    events.append("answer-wait")
                    return True

                def cancel(self):
                    events.append("answer-cancel")

            def fake_start(_settings, wav_bytes, **_kwargs):
                if wav_bytes == b"answer-wav":
                    self.assertIn("cue-wait", events)
                    events.append("answer-start")
                    return AnswerPlayback()
                events.append("cue-start")
                return CuePlayback()

            def fake_openclaw(*_args, **_kwargs):
                status, _ = StatusPublisherTests.read_status(directory)
                self.assertEqual(status["overall"]["stage"], "openclaw")
                self.assertEqual(status["pipeline"]["steps"]["openclaw"], "active")
                self.assertEqual(status["pipeline"]["steps"]["tts"], "idle")
                self.assertEqual(status["pipeline"]["steps"]["play"], "idle")
                self.assertEqual(events, ["cue-start"])
                events.append("claw")
                return "answer"

            played = self.run_command(
                bridge,
                {
                    "ask_openclaw": mock.Mock(side_effect=fake_openclaw),
                    "synthesize": mock.Mock(return_value=b"answer-wav"),
                    "start_playback": mock.Mock(side_effect=fake_start),
                },
            )
            status, _ = StatusPublisherTests.read_status(directory)
            bridge.status.stop()

        self.assertTrue(played)
        self.assertEqual(
            events,
            ["cue-start", "claw", "cue-wait", "answer-start", "answer-wait"],
        )
        self.assertIsNone(status["last_error"])

    def test_cue_failure_is_nonfatal_and_does_not_mark_playback_failed(self) -> None:
        bridge = self.module.VoiceBridge(self.settings())
        calls = 0

        def fake_start(_settings, _wav_bytes, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("private cue detail")
            return self.ImmediatePlayback()

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(
                self.run_command(
                    bridge,
                    {
                        "synthesize": mock.Mock(return_value=b"answer-wav"),
                        "start_playback": mock.Mock(side_effect=fake_start),
                    },
                )
            )
        self.assertEqual(calls, 2)
        self.assertIsNone(bridge.status._document["last_error"])
        self.assertEqual(bridge.status._document["pipeline"]["steps"]["play"], "active")

    def test_thinking_cue_is_bounded_quiet_and_faded(self) -> None:
        rate = 44_100
        frame_count = round(rate * 0.604)
        source = io.BytesIO()
        with self.module.wave.open(source, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(struct.pack(f"<{frame_count}h", *([20_000] * frame_count)))

        cue = self.module.soften_thinking_cue(source.getvalue())
        duration = self.module.validate_tts_wav(cue)
        with self.module.wave.open(io.BytesIO(cue), "rb") as wav:
            samples = struct.unpack(
                f"<{wav.getnframes()}h", wav.readframes(wav.getnframes())
            )

        self.assertLessEqual(duration, self.module.THINKING_CUE_MAX_SECONDS)
        self.assertLessEqual(
            max(abs(sample) for sample in samples),
            round(32_767 * self.module.THINKING_CUE_TARGET_PEAK),
        )
        self.assertEqual(samples[0], 0)
        self.assertEqual(samples[-1], 0)


class StatusPublisherTests(BridgeTestCase):
    @staticmethod
    def read_status(directory: str) -> tuple[dict, bytes]:
        raw = (pathlib.Path(directory) / "status.json").read_bytes()
        return json.loads(raw), raw

    def test_atomic_bounded_schema_and_normal_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
            publisher.start()
            first_inode = (pathlib.Path(directory) / "status.json").stat().st_ino
            publisher.ready()
            status, raw = self.read_status(directory)
            second_inode = (pathlib.Path(directory) / "status.json").stat().st_ino

            self.assertNotEqual(first_inode, second_inode)
            self.assertLessEqual(len(raw), self.module.STATUS_MAX_BYTES)
            self.assertEqual(status["schema"], 1)
            self.assertEqual(status["service"], "cerberus-voice")
            self.assertEqual(status["device"], "Cerberus")
            self.assertEqual(status["overall"]["state"], "ready")
            self.assertEqual(status["overall"]["stage"], "listening")
            self.assertRegex(status["instance_id"], r"^\d+-\d+$")
            self.assertEqual(status["heartbeat_at"], status["updated_at"])
            self.assertFalse(list(pathlib.Path(directory).glob("*.tmp")))

            publisher.stop()
            stopped, _ = self.read_status(directory)
            self.assertEqual(stopped["overall"]["state"], "stopped")
            self.assertEqual(stopped["overall"]["stage"], "stopped")
            self.assertEqual(stopped["wake_word"]["state"], "stopped")
            self.assertIsNotNone(stopped["stopped_at"])

    def test_dependency_health_is_content_free_and_recovers(self) -> None:
        private_error = "private dependency response payload"
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
            publisher.start()
            publisher.ready()
            for name in ("asr", "openclaw", "tts"):
                publisher.dependency_result(name)
            publisher.dependency_result("tts", TimeoutError(private_error))
            failed, raw = self.read_status(directory)
            publisher.dependency_result("tts")
            recovered, _ = self.read_status(directory)
            publisher.stop()

        self.assertEqual(failed["overall"]["state"], "degraded")
        self.assertEqual(failed["last_error"], {
            "stage": "tts_synthesis",
            "type": "TimeoutError",
            "at": failed["last_error"]["at"],
        })
        self.assertEqual(
            set(failed["dependencies"]), {"asr", "openclaw", "tts"}
        )
        self.assertEqual(failed["dependencies"]["tts"]["state"], "error")
        self.assertEqual(
            failed["dependencies"]["tts"]["error_type"], "TimeoutError"
        )
        self.assertNotIn(private_error.encode(), raw)
        self.assertIsNone(recovered["last_error"])
        self.assertEqual(recovered["overall"]["state"], "ready")

    def test_heartbeat_advances_without_resetting_stage_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=0.25)
            publisher.start()
            publisher.begin_openclaw()
            before, _ = self.read_status(directory)
            deadline = time.monotonic() + 1.5
            after = before
            while after["sequence"] == before["sequence"] and time.monotonic() < deadline:
                time.sleep(0.05)
                after, _ = self.read_status(directory)
            publisher.stop()

            self.assertGreater(after["sequence"], before["sequence"])
            self.assertGreater(after["updated_at_epoch"], before["updated_at_epoch"])
            self.assertEqual(
                after["overall"]["stage_started_at"],
                before["overall"]["stage_started_at"],
            )
            self.assertEqual(after["openclaw"]["state"], "thinking")

    def test_pipeline_progress_is_turn_scoped_monotonic_and_content_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
            publisher.start()
            publisher.ready()

            publisher.begin_asr()
            asr, _ = self.read_status(directory)
            publisher.component_ok("asr")
            publisher.wake_triggered(heard_now=True)
            accepted, _ = self.read_status(directory)
            publisher.begin_openclaw()
            claw, _ = self.read_status(directory)
            publisher.component_ok("openclaw")
            publisher.begin_tts(2)
            synthesis_one, _ = self.read_status(directory)
            publisher.tts_phase("playing", 1)
            playback_one, _ = self.read_status(directory)
            publisher.tts_phase("synthesizing", 2)
            synthesis_two, _ = self.read_status(directory)
            publisher.tts_phase("playing", 2)
            playback_two, _ = self.read_status(directory)
            publisher.begin_cooldown()
            complete, raw = self.read_status(directory)
            publisher.finish_tts()
            publisher.ready()
            idle, _ = self.read_status(directory)
            publisher.stop()

            self.assertEqual(asr["pipeline"]["steps"]["heard_name"], "idle")
            self.assertEqual(asr["pipeline"]["steps"]["asr"], "active")
            self.assertEqual(
                accepted["pipeline"]["steps"],
                {
                    "heard_name": "complete",
                    "asr": "complete",
                    "openclaw": "idle",
                    "tts": "idle",
                    "play": "idle",
                },
            )
            self.assertEqual(claw["pipeline"]["steps"]["openclaw"], "active")
            self.assertEqual(synthesis_one["pipeline"]["steps"]["tts"], "active")
            self.assertEqual(synthesis_one["pipeline"]["steps"]["play"], "idle")
            self.assertEqual(playback_one["pipeline"]["steps"]["play"], "active")
            self.assertEqual(synthesis_two["pipeline"]["steps"]["play"], "active")
            self.assertEqual(synthesis_two["tts"]["synthesis_chunk_index"], 2)
            self.assertEqual(synthesis_two["tts"]["playback_chunk_index"], 1)
            self.assertEqual(playback_two["pipeline"]["steps"]["tts"], "complete")
            self.assertEqual(playback_two["tts"]["synthesis_chunk_index"], 2)
            self.assertEqual(playback_two["tts"]["playback_chunk_index"], 2)
            self.assertTrue(all(
                state == "complete"
                for state in complete["pipeline"]["steps"].values()
            ))
            self.assertEqual(complete["pipeline"]["mode"], "complete")
            self.assertFalse(idle["pipeline"]["active"])
            self.assertEqual(idle["pipeline"]["mode"], "idle")
            self.assertNotIn("transcript", raw.decode("utf-8"))
            self.assertNotIn("response", raw.decode("utf-8"))

    def test_pipeline_preserves_heard_name_across_armed_followup_asr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
            publisher.start()
            publisher.ready()
            publisher.begin_asr()
            publisher.component_ok("asr")
            publisher.wake_armed(12)
            armed, _ = self.read_status(directory)
            publisher.begin_asr()
            followup, _ = self.read_status(directory)
            publisher.component_ok("asr")
            publisher.wake_triggered(heard_now=False)
            accepted, _ = self.read_status(directory)
            publisher.stop()

            self.assertEqual(armed["pipeline"]["mode"], "armed")
            self.assertEqual(armed["pipeline"]["steps"]["heard_name"], "complete")
            self.assertEqual(armed["pipeline"]["steps"]["asr"], "idle")
            self.assertEqual(followup["pipeline"]["steps"]["heard_name"], "complete")
            self.assertEqual(followup["pipeline"]["steps"]["asr"], "active")
            self.assertEqual(accepted["pipeline"]["steps"]["asr"], "complete")

    def test_status_never_contains_private_pipeline_content(self) -> None:
        private_transcript = "Cerberus PRIVATE SPOKEN COMMAND 91a6"
        private_answer = "PRIVATE MODEL ANSWER 782b"
        private_token = "PRIVATE-BEARER-TOKEN-5c0d"
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.module.VoiceBridge(
                self.settings(state_dir=directory, openclaw_token=private_token)
            )
            bridge.status.start()

            observed = []

            def at_asr(*_args, **_kwargs):
                current, _ = self.read_status(directory)
                observed.append(
                    (
                        current["overall"]["stage"],
                        current["wake_word"]["state"],
                        current["asr"]["state"],
                    )
                )
                return private_transcript

            def at_openclaw(*_args, **_kwargs):
                current, _ = self.read_status(directory)
                observed.append(
                    (current["overall"]["stage"], current["openclaw"]["state"])
                )
                return private_answer

            def at_synthesis(*_args):
                current, _ = self.read_status(directory)
                observed.append(
                    (
                        current["overall"]["stage"],
                        current["tts"]["state"],
                        current["tts"]["chunk_index"],
                        current["tts"]["chunk_total"],
                    )
                )
                return b"wav"

            def at_playback(*_args, **_kwargs):
                if _args[1] != b"wav":
                    return self.ImmediatePlayback()
                current, _ = self.read_status(directory)
                observed.append(
                    (
                        current["overall"]["stage"],
                        current["tts"]["state"],
                        current["tts"]["chunk_index"],
                        current["tts"]["chunk_total"],
                    )
                )
                return self.ImmediatePlayback()

            with (
                mock.patch.object(self.module, "transcribe_wav", side_effect=at_asr),
                mock.patch.object(self.module, "ask_openclaw", side_effect=at_openclaw),
                mock.patch.object(self.module, "synthesize", side_effect=at_synthesis),
                mock.patch.object(
                    self.module, "start_playback", side_effect=at_playback
                ),
                mock.patch.object(
                    self.module,
                    "SynthesisWorker",
                    self.inline_synthesis_worker_class(),
                ),
            ):
                self.assertTrue(bridge.handle_utterance(b"pcm"))
            bridge.status.begin_cooldown()
            cooldown, _ = self.read_status(directory)
            bridge.status.finish_tts()
            bridge.status.ready()
            status, raw = self.read_status(directory)
            bridge.status.stop()

            serialized = raw.decode("utf-8")
            self.assertNotIn(private_transcript, serialized)
            self.assertNotIn(private_answer, serialized)
            self.assertNotIn(private_token, serialized)
            self.assertEqual(
                observed,
                [
                    ("asr", "checking", "processing"),
                    ("openclaw", "thinking"),
                    ("tts_synthesis", "synthesizing", 1, 1),
                    ("tts_playback", "playing", 1, 1),
                ],
            )
            self.assertEqual(cooldown["overall"]["stage"], "cooldown")
            self.assertEqual(cooldown["tts"]["state"], "cooldown")
            self.assertEqual(status["wake_word"]["state"], "listening")
            self.assertIsNotNone(status["wake_word"]["last_trigger_at"])
            self.assertEqual(status["asr"]["state"], "ok")
            self.assertEqual(status["openclaw"]["state"], "ok")
            self.assertEqual(status["tts"]["state"], "ok")
            self.assertEqual(status["tts"]["chunk_index"], 1)
            self.assertEqual(status["tts"]["chunk_total"], 1)
            self.assertIsInstance(status["tts"]["duration_seconds"], float)
            self.assertIsNone(status["last_error"])

    def test_failure_stage_and_type_are_recorded_without_error_message(self) -> None:
        private_error = "PRIVATE ERROR BODY f17f"
        scenarios = {
            "asr": (
                {"transcribe_wav": mock.Mock(side_effect=RuntimeError(private_error))},
                "asr",
                "asr",
            ),
            "openclaw": (
                {
                    "transcribe_wav": mock.Mock(
                        return_value="Cerberus private request"
                    ),
                    "ask_openclaw": mock.Mock(side_effect=TimeoutError(private_error)),
                    "start_playback": mock.Mock(
                        side_effect=self.immediate_playback
                    ),
                },
                "openclaw",
                "openclaw",
            ),
            "tts_synthesis": (
                {
                    "transcribe_wav": mock.Mock(
                        return_value="Cerberus private request"
                    ),
                    "ask_openclaw": mock.Mock(return_value="private answer"),
                    "synthesize": mock.Mock(side_effect=ValueError(private_error)),
                    "start_playback": mock.Mock(
                        side_effect=self.immediate_playback
                    ),
                },
                "tts_synthesis",
                "tts",
            ),
            "tts_playback": (
                {
                    "transcribe_wav": mock.Mock(
                        return_value="Cerberus private request"
                    ),
                    "ask_openclaw": mock.Mock(return_value="private answer"),
                    "synthesize": mock.Mock(return_value=b"wav"),
                    "start_playback": mock.Mock(side_effect=OSError(private_error)),
                },
                "tts_playback",
                "play",
            ),
        }
        for name, (patches, expected_stage, expected_step) in scenarios.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
                bridge.status.start()
                patchers = [
                    mock.patch.object(self.module, key, value)
                    for key, value in patches.items()
                ]
                patchers.append(
                    mock.patch.object(
                        self.module,
                        "SynthesisWorker",
                        self.inline_synthesis_worker_class(),
                    )
                )
                for patcher in patchers:
                    patcher.start()
                try:
                    with self.assertRaises(Exception):
                        bridge.handle_utterance(b"pcm")
                finally:
                    for patcher in reversed(patchers):
                        patcher.stop()
                status, raw = self.read_status(directory)
                bridge.status.stop()

                self.assertEqual(status["overall"]["state"], "degraded")
                self.assertEqual(status["overall"]["stage"], "retry_wait")
                self.assertEqual(status["last_error"]["stage"], expected_stage)
                self.assertEqual(status["pipeline"]["mode"], "error")
                self.assertFalse(status["pipeline"]["active"])
                self.assertEqual(status["pipeline"]["steps"][expected_step], "error")
                self.assertNotIn("active", status["pipeline"]["steps"].values())
                self.assertRegex(status["last_error"]["type"], r"^[A-Za-z_][A-Za-z0-9_]*$")
                self.assertNotIn(private_error, raw.decode("utf-8"))

    def test_empty_spoken_chunk_set_returns_to_listening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
            bridge.status.start()
            with (
                mock.patch.object(
                    self.module,
                    "transcribe_wav",
                    return_value="Cerberus answer silently",
                ),
                mock.patch.object(self.module, "ask_openclaw", return_value="answer"),
                mock.patch.object(
                    self.module, "bounded_spoken_chunks", return_value=([], False)
                ),
                mock.patch.object(self.module, "synthesize") as synthesize_mock,
                mock.patch.object(
                    self.module,
                    "start_playback",
                    side_effect=self.immediate_playback,
                ) as playback_mock,
            ):
                self.assertFalse(bridge.handle_utterance(b"pcm"))
            status, _ = self.read_status(directory)
            bridge.status.stop()

            synthesize_mock.assert_not_called()
            # The courtesy cue is independent of response TTS and remains
            # allowed even when OpenClaw returns no speakable chunks.
            self.assertEqual(playback_mock.call_count, 1)
            self.assertEqual(status["overall"]["state"], "ready")
            self.assertEqual(status["overall"]["stage"], "listening")
            self.assertEqual(status["wake_word"]["state"], "listening")
            self.assertEqual(status["tts"]["state"], "ok")
            self.assertEqual(status["tts"]["chunk_index"], 0)
            self.assertEqual(status["tts"]["chunk_total"], 0)
            self.assertIsNotNone(status["tts"]["completed_at"])

    def test_empty_asr_does_not_hide_or_consume_a_live_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
            bridge.status.start()
            bridge.status.ready()
            with (
                mock.patch.object(
                    self.module,
                    "transcribe_wav",
                    side_effect=["Cerberus", "", "What is the cluster status?"],
                ),
                mock.patch.object(self.module, "ask_openclaw", return_value="Healthy."),
                mock.patch.object(self.module, "synthesize", return_value=b"wav"),
                mock.patch.object(
                    self.module,
                    "start_playback",
                    side_effect=self.immediate_playback,
                ),
                mock.patch.object(
                    self.module,
                    "SynthesisWorker",
                    self.inline_synthesis_worker_class(),
                ),
            ):
                self.assertFalse(bridge.handle_utterance(b"wake"))
                self.assertFalse(bridge.handle_utterance(b"empty"))
                still_armed, _ = self.read_status(directory)
                self.assertEqual(still_armed["overall"]["state"], "armed")
                self.assertEqual(still_armed["wake_word"]["state"], "armed")
                self.assertIsNotNone(still_armed["wake_word"]["armed_until"])
                self.assertEqual(
                    still_armed["pipeline"],
                    {
                        "active": True,
                        "mode": "armed",
                        "steps": {
                            "heard_name": "complete",
                            "asr": "idle",
                            "openclaw": "idle",
                            "tts": "idle",
                            "play": "idle",
                        },
                    },
                )
                self.assertTrue(bridge.handle_utterance(b"command"))
            bridge.status.stop()

    def test_failed_asr_retry_can_preserve_a_live_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
            bridge.status.start()
            bridge.status.ready()
            with (
                mock.patch.object(
                    self.module,
                    "transcribe_wav",
                    side_effect=[
                        "Cerberus",
                        self.module.urllib.error.URLError("private"),
                        "Report status",
                    ],
                ),
                mock.patch.object(self.module, "ask_openclaw", return_value="Healthy."),
                mock.patch.object(self.module, "synthesize", return_value=b"wav"),
                mock.patch.object(
                    self.module,
                    "start_playback",
                    side_effect=self.immediate_playback,
                ),
                mock.patch.object(
                    self.module,
                    "SynthesisWorker",
                    self.inline_synthesis_worker_class(),
                ),
            ):
                self.assertFalse(bridge.handle_utterance(b"wake"))
                with self.assertRaises(self.module.urllib.error.URLError):
                    bridge.handle_utterance(b"failed-asr")
                # This is the recovery transition used by run() after its
                # bounded retry delay.
                bridge.status.resume_listening()
                retry_status, _ = self.read_status(directory)
                self.assertEqual(retry_status["overall"]["state"], "armed")
                self.assertEqual(retry_status["last_error"]["stage"], "asr")
                self.assertEqual(retry_status["pipeline"]["mode"], "armed")
                self.assertEqual(
                    retry_status["pipeline"]["steps"],
                    {
                        "heard_name": "complete",
                        "asr": "error",
                        "openclaw": "idle",
                        "tts": "idle",
                        "play": "idle",
                    },
                )
                self.assertTrue(bridge.handle_utterance(b"command"))
                recovered, _ = self.read_status(directory)
                self.assertIsNone(recovered["last_error"])
                self.assertEqual(recovered["pipeline"]["steps"]["asr"], "complete")
            bridge.status.stop()

    def test_armed_state_expires_in_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=0.25)
            publisher.start()
            publisher.wake_armed(0.05)
            deadline = time.monotonic() + 1.5
            status, _ = self.read_status(directory)
            while status["wake_word"]["state"] == "armed" and time.monotonic() < deadline:
                time.sleep(0.05)
                status, _ = self.read_status(directory)
            publisher.stop()
            self.assertEqual(status["wake_word"]["state"], "listening")
            self.assertIsNone(status["wake_word"]["armed_until"])

    def test_relative_state_directory_is_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "absolute"):
            self.module.StatusPublisher("relative/status")

    def test_rejected_vad_burst_returns_to_listening_without_losing_arm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
            publisher.start()
            publisher.ready()
            publisher.speech_detected()
            publisher.resume_listening()
            listening, _ = self.read_status(directory)
            self.assertEqual(listening["overall"], {
                "state": "ready",
                "stage": "listening",
                "stage_started_at": listening["overall"]["stage_started_at"],
            })
            self.assertEqual(listening["wake_word"]["state"], "listening")

            publisher.wake_armed(12)
            armed_until = self.read_status(directory)[0]["wake_word"]["armed_until"]
            publisher.speech_detected()
            publisher.resume_listening()
            armed, _ = self.read_status(directory)
            publisher.stop()

            self.assertEqual(armed["overall"]["state"], "armed")
            self.assertEqual(armed["overall"]["stage"], "listening")
            self.assertEqual(armed["wake_word"]["state"], "armed")
            self.assertEqual(armed["wake_word"]["armed_until"], armed_until)

    def test_failure_persists_until_the_same_stage_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
            publisher.start()
            publisher.fail("asr", TimeoutError("private"))
            publisher.ready()
            retained, _ = self.read_status(directory)
            self.assertEqual(retained["last_error"]["stage"], "asr")
            self.assertEqual(retained["pipeline"]["steps"]["asr"], "error")

            publisher.begin_openclaw()
            publisher.component_ok("openclaw")
            unrelated, _ = self.read_status(directory)
            self.assertEqual(unrelated["last_error"]["stage"], "asr")

            publisher.begin_asr()
            publisher.component_ok("asr")
            recovered, _ = self.read_status(directory)
            publisher.stop()
            self.assertIsNone(recovered["last_error"])

    def test_unrelated_listening_pass_restores_retained_failed_band(self) -> None:
        scenarios = {
            "openclaw": "openclaw",
            "tts_synthesis": "tts",
            "tts_playback": "play",
        }
        for failure_stage, failed_step in scenarios.items():
            with self.subTest(stage=failure_stage), tempfile.TemporaryDirectory() as directory:
                publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
                publisher.start()
                publisher.ready()
                publisher.fail(failure_stage, TimeoutError("private"))

                # An unrelated utterance reaches ASR but does not address the
                # retained downstream failure and is then rejected.
                publisher.begin_asr()
                publisher.component_ok("asr")
                publisher.wake_not_detected()
                ignored, _ = self.read_status(directory)

                # A rejected VAD burst follows the same listening recovery
                # path and must not erase the red failed band either.
                publisher.speech_detected()
                publisher.resume_listening()
                resumed, _ = self.read_status(directory)
                publisher.stop()

                for status in (ignored, resumed):
                    self.assertEqual(status["last_error"]["stage"], failure_stage)
                    self.assertFalse(status["pipeline"]["active"])
                    self.assertEqual(status["pipeline"]["mode"], "error")
                    self.assertEqual(
                        status["pipeline"]["steps"][failed_step], "error"
                    )
                    self.assertNotIn("active", status["pipeline"]["steps"].values())

    def test_chunked_tts_failures_leave_no_active_step(self) -> None:
        scenarios = {
            "tts_playback": ("play", "tts"),
            "tts_synthesis": ("tts", "play"),
        }
        for failure_stage, (failed_step, completed_step) in scenarios.items():
            with self.subTest(stage=failure_stage), tempfile.TemporaryDirectory() as directory:
                publisher = self.module.StatusPublisher(directory, heartbeat_seconds=30)
                publisher.start()
                publisher.ready()
                publisher.begin_tts(2)
                publisher.tts_phase("playing", 1)
                if failure_stage == "tts_synthesis":
                    publisher.tts_phase("synthesizing", 2)
                publisher.fail(failure_stage, TimeoutError("private"))
                failed, _ = self.read_status(directory)
                publisher.stop()

                self.assertFalse(failed["pipeline"]["active"])
                self.assertEqual(failed["pipeline"]["mode"], "error")
                self.assertEqual(
                    failed["pipeline"]["steps"][failed_step], "error"
                )
                self.assertEqual(
                    failed["pipeline"]["steps"][completed_step], "complete"
                )
                self.assertNotIn("active", failed["pipeline"]["steps"].values())

    def test_capture_failure_maps_to_asr_without_faking_wake_activity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            publisher = self.module.StatusPublisher(directory, heartbeat_seconds=0.25)
            publisher.start()
            publisher.ready()
            publisher.speech_detected()
            publisher.fail("capture", OSError("private device detail"))
            publisher.resume_listening()
            failed, _ = self.read_status(directory)

            deadline = time.monotonic() + 1.5
            heartbeat = failed
            while (
                heartbeat["sequence"] == failed["sequence"]
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
                heartbeat, _ = self.read_status(directory)

            publisher.begin_asr()
            recovering, _ = self.read_status(directory)
            publisher.stop()

            self.assertEqual(failed["last_error"]["stage"], "capture")
            self.assertEqual(failed["pipeline"]["mode"], "error")
            self.assertFalse(failed["pipeline"]["active"])
            self.assertEqual(failed["pipeline"]["steps"]["heard_name"], "idle")
            self.assertEqual(failed["pipeline"]["steps"]["asr"], "error")
            self.assertIsNone(failed["wake_word"]["last_trigger_at"])
            self.assertGreater(heartbeat["sequence"], failed["sequence"])
            self.assertFalse(heartbeat["pipeline"]["active"])
            self.assertIsNone(heartbeat["wake_word"]["last_trigger_at"])
            self.assertIsNone(recovering["last_error"])
            self.assertEqual(recovering["pipeline"]["mode"], "scanning")
            self.assertEqual(recovering["pipeline"]["steps"]["asr"], "active")


if __name__ == "__main__":
    unittest.main()
