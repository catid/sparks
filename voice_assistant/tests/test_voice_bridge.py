from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import struct
import sys
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


if __name__ == "__main__":
    unittest.main()
