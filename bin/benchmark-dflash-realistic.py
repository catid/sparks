#!/usr/bin/env python3
"""Measure DFlash on deterministic, representative prose and code prompts."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROMPTS = [
    {
        "name": "python_patch",
        "content": """Write a complete dependency-free Python function
`atomic_update_json(path, transform)` that locks against other processes,
preserves file permissions, fsyncs both the replacement and parent directory,
and leaves the old file intact if transform or serialization fails. Explain
the important failure cases after the code. Do not use placeholders.""",
    },
    {
        "name": "incident_reasoning",
        "content": """An API has 8 workers, a database pool of 4 connections,
four database waiters, repeated db_pool_timeout errors, CPU at 31%, and a
healthy database at 21 ms p95. Produce a concise incident analysis with the
smallest safe configuration change, verification steps, and rollback. Do not
invent missing evidence.""",
    },
    {
        "name": "agent_plan",
        "content": """You are maintaining a local inference service. Give a
concrete, ordered shell-level diagnostic plan for intermittent malformed JSON
tool calls. Cover reproduction, log capture, tokenizer/chat-template checks,
request isolation, regression tests, and safe rollback. Include example
commands, but do not assume Kubernetes.""",
    },
]

METRICS = (
    "vllm:spec_decode_num_drafts_total",
    "vllm:spec_decode_num_draft_tokens_total",
    "vllm:spec_decode_num_accepted_tokens_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
)


def request_json(method: str, url: str, body: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None,
                 timeout: int = 900) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def request_text(url: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode()


def metric_snapshot(endpoint: str) -> dict[str, float]:
    text = request_text(f"{endpoint}/metrics")
    snapshot: dict[str, float] = {}
    for name in METRICS:
        matches = re.findall(
            rf"^{re.escape(name)}(?:\{{[^}}]*\}})? ([0-9.eE+-]+)$",
            text,
            flags=re.MULTILINE,
        )
        snapshot[name] = sum(float(value) for value in matches)
    return snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=768)
    args = parser.parse_args()

    model = request_json("GET", f"{args.endpoint}/v1/models")["data"][0]["id"]
    before = metric_snapshot(args.endpoint)
    requests = []
    for repeat in range(args.repeats):
        for prompt in PROMPTS:
            request_id = f"dflash-real-{uuid.uuid4()}"
            body = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Answer the request directly and precisely. Check "
                            "edge cases before finalizing."
                        ),
                    },
                    {"role": "user", "content": prompt["content"]},
                ],
                "temperature": 0,
                "max_tokens": args.max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            started = time.perf_counter()
            response = request_json(
                "POST",
                f"{args.endpoint}/v1/chat/completions",
                body,
                headers={
                    "X-Session-ID": request_id,
                    "X-Request-ID": request_id,
                },
            )
            elapsed = time.perf_counter() - started
            choice = response["choices"][0]
            usage = response.get("usage") or {}
            completion_tokens = int(usage.get("completion_tokens", 0))
            requests.append({
                "prompt": prompt["name"],
                "repeat": repeat + 1,
                "elapsed_seconds": elapsed,
                "completion_tokens": completion_tokens,
                "output_tokens_per_second": (
                    completion_tokens / elapsed if elapsed else None
                ),
                "finish_reason": choice.get("finish_reason"),
                "output": choice["message"].get("content"),
            })
            print(
                f"{prompt['name']} repeat={repeat + 1}: "
                f"{completion_tokens} tokens in {elapsed:.2f}s "
                f"({completion_tokens / elapsed:.2f} tok/s)",
                flush=True,
            )

    after = metric_snapshot(args.endpoint)
    delta = {key: after[key] - before[key] for key in METRICS}
    drafts = delta["vllm:spec_decode_num_drafts_total"]
    draft_tokens = delta["vllm:spec_decode_num_draft_tokens_total"]
    accepted = delta["vllm:spec_decode_num_accepted_tokens_total"]
    result = {
        "label": args.label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": args.endpoint,
        "model": model,
        "configuration": {
            "repeats": args.repeats,
            "max_tokens": args.max_tokens,
            "temperature": 0,
            "thinking": False,
        },
        "requests": requests,
        "metrics_delta": delta,
        "accepted_tokens_per_draft_step": accepted / drafts if drafts else None,
        "draft_token_acceptance_percent": (
            100 * accepted / draft_tokens if draft_tokens else None
        ),
        "aggregate_output_tokens_per_second": (
            sum(item["completion_tokens"] for item in requests)
            / sum(item["elapsed_seconds"] for item in requests)
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "label": args.label,
        "aggregate_output_tokens_per_second":
            result["aggregate_output_tokens_per_second"],
        "accepted_tokens_per_draft_step":
            result["accepted_tokens_per_draft_step"],
        "draft_token_acceptance_percent":
            result["draft_token_acceptance_percent"],
        "output": str(args.output),
    }, indent=2))


if __name__ == "__main__":
    main()
