#!/usr/bin/env python3
"""Verify that a live Laguna backend accepts >32K input and a 32K output cap."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--filler-words", type=int, default=40_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    with urllib.request.urlopen(f"{args.endpoint}/v1/models", timeout=30) as response:
        model = json.load(response)["data"][0]["id"]

    marker = "ORCHID-7391"
    filler = "foo " * args.filler_words
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Follow the final instruction and answer with only the requested code.",
            },
            {
                "role": "user",
                "content": (
                    f"Remember this code: {marker}.\n{filler}\n"
                    "What code were you asked to remember? Reply with only the code."
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": 32768,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    request = urllib.request.Request(
        f"{args.endpoint}/v1/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=900) as response:
        result = json.load(response)
    elapsed = time.perf_counter() - started
    usage = result.get("usage") or {}
    output = result["choices"][0]["message"].get("content") or ""
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    if prompt_tokens <= 32768:
        raise SystemExit(
            f"prompt was only {prompt_tokens} tokens; expected more than 32768"
        )
    if marker not in output:
        raise SystemExit(f"model did not retrieve marker; output was {output!r}")
    validation = {
        "endpoint": args.endpoint,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "requested_max_output_tokens": 32768,
        "actual_completion_tokens": int(usage.get("completion_tokens", 0)),
        "finish_reason": result["choices"][0].get("finish_reason"),
        "output": output,
        "elapsed_seconds": elapsed,
    }
    rendered = json.dumps(validation, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
