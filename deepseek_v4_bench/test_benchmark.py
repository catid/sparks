#!/usr/bin/env python3
"""Network-free unit tests for benchmark.py."""

from __future__ import annotations

import json
import math
import unittest

import benchmark


class PromptTests(unittest.TestCase):
    def test_prompt_variants_are_distinct_and_scoped(self) -> None:
        first = benchmark.build_messages(0, 0)
        second = benchmark.build_messages(1, 0)
        self.assertNotEqual(first, second)
        self.assertIn("/workspace/queuepilot", first[0]["content"])
        self.assertIn("cancellation race", first[1]["content"])

    def test_padding_word_count(self) -> None:
        text = benchmark.padding_text(3, 25)
        body = text.rsplit("\n", 1)[-1]
        self.assertEqual(len(body.split()), 25)

    def test_payload_has_required_benchmark_controls(self) -> None:
        payload = benchmark.build_chat_payload(
            "model", benchmark.build_messages(0, 0), 1024, 123
        )
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertEqual(payload["min_tokens"], 1024)
        self.assertTrue(payload["ignore_eos"])
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 1.0)
        self.assertTrue(payload["stream_options"]["include_usage"])
        self.assertEqual(payload["tool_choice"], "auto")

    def test_authorization_is_never_persisted(self) -> None:
        self.assertIn("Authorization", benchmark.safe_headers("secret", "id"))
        self.assertNotIn("Authorization", benchmark.persisted_headers("id"))


class SSETests(unittest.TestCase):
    def test_fragmented_sse_reconstruction_and_usage(self) -> None:
        stream = benchmark.SSEAccumulator(started=10.0)
        event1 = {
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning": "check "},
                    "finish_reason": None,
                }
            ]
        }
        event2 = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "exec",
                                    "arguments": '{"command":"pytest"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "length",
                }
            ]
        }
        usage = {
            "choices": [],
            "usage": {"prompt_tokens": 1024, "completion_tokens": 1024},
        }
        raw = (
            b"data: " + json.dumps(event1).encode() + b"\r\n\r\n"
            b"data: " + json.dumps(event2).encode() + b"\n\n"
            b"data: " + json.dumps(usage).encode() + b"\n\n"
            b"data: [DONE]\n\n"
        )
        stream.feed(raw[:17], 10.2)
        stream.feed(raw[17:83], 10.3)
        stream.feed(raw[83:], 10.5)
        stream.finish(10.5)
        self.assertEqual(bytes(stream.raw), raw)
        self.assertEqual(stream.usage["completion_tokens"], 1024)
        self.assertEqual(stream.finish_reasons, ["length"])
        self.assertEqual(stream.reconstructed()["reasoning"], "check ")
        self.assertEqual(
            stream.reconstructed()["tool_calls"][0]["function"]["name"], "exec"
        )
        self.assertTrue(stream.saw_done)
        # TTFT is timestamped when the first complete SSE event arrives. This
        # synthetic event's delimiter is in the final fragment.
        self.assertTrue(math.isclose(stream.first_output_at or 0, 10.5))

    def test_invalid_json_is_preserved_as_error(self) -> None:
        stream = benchmark.SSEAccumulator(started=1.0)
        stream.feed(b"data: {bad json}\n\n", 1.2)
        self.assertEqual(len(stream.events), 1)
        self.assertEqual(len(stream.parse_errors), 1)


class StatisticsTests(unittest.TestCase):
    def test_percentile_interpolation(self) -> None:
        self.assertEqual(benchmark.percentile([1.0, 3.0], 0.5), 2.0)
        self.assertIsNone(benchmark.percentile([], 0.95))

    def test_natural_stop_skips_exact_completion_length_validation(self) -> None:
        self.assertIsNone(benchmark.completion_length_error(122, None))
        self.assertIsNone(benchmark.completion_length_error(1024, 1024))
        self.assertEqual(
            benchmark.completion_length_error(122, 1024),
            "completion length 122, expected 1024",
        )


if __name__ == "__main__":
    unittest.main()
