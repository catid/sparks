from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "audio8_stock_health", ROOT / "check_stock_health.py"
)
assert SPEC and SPEC.loader
stock_health = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stock_health)


def healthy_payload() -> dict[str, object]:
    return {
        "status": "ok",
        "model": "audio8/tts-0.6b",
        "synthetic_audio": True,
        "reference_conditioned": True,
        "reference_conditioning_cached": True,
        "sample_rate": 44_100,
        "sdpa_backend": "efficient",
        "codebook_compile_requested": True,
        "codebook_compile_active": True,
        "codebook_compile_state": "compiled",
    }


class StockHealthTests(unittest.TestCase):
    def test_exact_optimized_contract_passes(self) -> None:
        with mock.patch.object(stock_health, "get_json", return_value=healthy_payload()):
            stock_health.check_stock()

    def test_every_required_field_fails_closed(self) -> None:
        expected = healthy_payload()
        for key in tuple(expected):
            with self.subTest(key=key):
                invalid = dict(expected)
                invalid[key] = None
                with mock.patch.object(stock_health, "get_json", return_value=invalid):
                    with self.assertRaisesRegex(RuntimeError, "not production-ready"):
                        stock_health.check_stock()


if __name__ == "__main__":
    unittest.main()
