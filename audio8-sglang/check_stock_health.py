#!/usr/bin/env python3
"""Validate the exact optimized stock Audio8 rollback health contract."""

from __future__ import annotations

import json
import sys

from check_health import get_json


def check_stock(url: str = "http://127.0.0.1:8010/health") -> None:
    payload = get_json(url)
    if (
        payload.get("status") != "ok"
        or payload.get("model") != "audio8/tts-0.6b"
        or payload.get("synthetic_audio") is not True
        or payload.get("reference_conditioned") is not True
        or payload.get("reference_conditioning_cached") is not True
        or payload.get("sample_rate") != 44_100
        or payload.get("sdpa_backend") != "efficient"
        or payload.get("codebook_compile_requested") is not True
        or payload.get("codebook_compile_active") is not True
        or payload.get("codebook_compile_state") != "compiled"
    ):
        raise RuntimeError("stock health contract is not production-ready")


def main() -> None:
    if len(sys.argv) != 1:
        raise SystemExit("usage: check_stock_health.py")
    try:
        check_stock()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Stock Audio8 health check failed: {error}") from error


if __name__ == "__main__":
    main()
