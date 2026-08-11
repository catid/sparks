from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import struct
import sys
import tempfile
import time
import unittest
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
            "openclaw_user": "cerebrus3-voice",
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
            router.route("Cerebrus: summarize the latest run", now=30),
            ("summarize the latest run", "command"),
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
        router.route("Cerebrus", now=10)
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
                "TEST_URL", "http://10.10.84.28:8889/v1/chat/completions"
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
            mock.patch.object(self.module, "play_wav"),
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
            mock.patch.object(self.module, "play_wav"),
            contextlib.redirect_stdout(output),
        ):
            self.assertTrue(bridge.handle_utterance(b"pcm"))

        self.assertLessEqual(len(synthesized_chunks), self.module.MAX_TTS_CHUNKS)
        logged = output.getvalue()
        self.assertIn("response truncated for speech", logged)
        self.assertNotIn("private-model-output", logged)


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

            def at_asr(*_args):
                current, _ = self.read_status(directory)
                observed.append(
                    (
                        current["overall"]["stage"],
                        current["wake_word"]["state"],
                        current["asr"]["state"],
                    )
                )
                return private_transcript

            def at_openclaw(*_args):
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

            def at_playback(*_args):
                current, _ = self.read_status(directory)
                observed.append(
                    (
                        current["overall"]["stage"],
                        current["tts"]["state"],
                        current["tts"]["chunk_index"],
                        current["tts"]["chunk_total"],
                    )
                )

            with (
                mock.patch.object(self.module, "transcribe_wav", side_effect=at_asr),
                mock.patch.object(self.module, "ask_openclaw", side_effect=at_openclaw),
                mock.patch.object(self.module, "synthesize", side_effect=at_synthesis),
                mock.patch.object(self.module, "play_wav", side_effect=at_playback),
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
            ),
            "openclaw": (
                {
                    "transcribe_wav": mock.Mock(
                        return_value="Cerberus private request"
                    ),
                    "ask_openclaw": mock.Mock(side_effect=TimeoutError(private_error)),
                },
                "openclaw",
            ),
            "tts_synthesis": (
                {
                    "transcribe_wav": mock.Mock(
                        return_value="Cerberus private request"
                    ),
                    "ask_openclaw": mock.Mock(return_value="private answer"),
                    "synthesize": mock.Mock(side_effect=ValueError(private_error)),
                },
                "tts_synthesis",
            ),
            "tts_playback": (
                {
                    "transcribe_wav": mock.Mock(
                        return_value="Cerberus private request"
                    ),
                    "ask_openclaw": mock.Mock(return_value="private answer"),
                    "synthesize": mock.Mock(return_value=b"wav"),
                    "play_wav": mock.Mock(side_effect=OSError(private_error)),
                },
                "tts_playback",
            ),
        }
        for name, (patches, expected_stage) in scenarios.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                bridge = self.module.VoiceBridge(self.settings(state_dir=directory))
                bridge.status.start()
                patchers = [mock.patch.object(self.module, key, value) for key, value in patches.items()]
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
                mock.patch.object(self.module, "play_wav") as playback_mock,
            ):
                self.assertFalse(bridge.handle_utterance(b"pcm"))
            status, _ = self.read_status(directory)
            bridge.status.stop()

            synthesize_mock.assert_not_called()
            playback_mock.assert_not_called()
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
                mock.patch.object(self.module, "play_wav"),
            ):
                self.assertFalse(bridge.handle_utterance(b"wake"))
                self.assertFalse(bridge.handle_utterance(b"empty"))
                still_armed, _ = self.read_status(directory)
                self.assertEqual(still_armed["overall"]["state"], "armed")
                self.assertEqual(still_armed["wake_word"]["state"], "armed")
                self.assertIsNotNone(still_armed["wake_word"]["armed_until"])
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
                mock.patch.object(self.module, "play_wav"),
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
                self.assertTrue(bridge.handle_utterance(b"command"))
                recovered, _ = self.read_status(directory)
                self.assertIsNone(recovered["last_error"])
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

            publisher.begin_openclaw()
            publisher.component_ok("openclaw")
            unrelated, _ = self.read_status(directory)
            self.assertEqual(unrelated["last_error"]["stage"], "asr")

            publisher.begin_asr()
            publisher.component_ok("asr")
            recovered, _ = self.read_status(directory)
            publisher.stop()
            self.assertIsNone(recovered["last_error"])


if __name__ == "__main__":
    unittest.main()
