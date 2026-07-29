#!/usr/bin/env python3
"""Render and count the DeepSeek-V4 benchmark prompts without inference."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from prompts import build_prompts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("/home/catid/models/DeepSeek-V4-Flash-NVFP4"),
    )
    parser.add_argument("--target-input-tokens", type=int, default=1024)
    parser.add_argument("--tolerance", type=int, default=4)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally save exact calibrated messages and token metadata.",
    )
    args = parser.parse_args()

    prompts = build_prompts(
        args.model_path,
        target_input_tokens=args.target_input_tokens,
        tolerance=args.tolerance,
    )
    result = [
        {
            "name": item.name,
            "messages": item.messages,
            "input_tokens": item.input_tokens,
            "target_input_tokens": item.target_input_tokens,
            "token_delta": item.token_delta,
            "rendered_prompt_sha256": item.rendered_prompt_sha256,
            "calibration_method": item.calibration_method,
        }
        for item in prompts
    ]
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
