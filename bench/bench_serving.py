#!/usr/bin/env python3
"""Fixed-length streaming benchmark for one PP endpoint or two replicas."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp


@dataclass
class RequestResult:
    concurrency: int
    request_id: int
    endpoint: str
    prompt_tokens: int
    output_tokens: int
    ttft_s: float
    e2e_s: float
    tpot_s: float
    ok: bool
    error: str = ""


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - index) + ordered[hi] * (index - lo)


async def get_model(session: aiohttp.ClientSession, endpoint: str) -> str:
    async with session.get(f"{endpoint}/v1/models") as response:
        response.raise_for_status()
        payload = await response.json()
        return payload["data"][0]["id"]


async def one_request(
    session: aiohttp.ClientSession,
    endpoint: str,
    model: str,
    concurrency: int,
    request_id: int,
    input_tokens: int,
    output_tokens: int,
) -> RequestResult:
    # Valid, distinct token-ID prompts avoid tokenizer ambiguity and shared-prefix
    # cache effects. Laguna's vocabulary is much larger than this range.
    prompt = [
        1000 + ((request_id * 1543 + position * 7919) % 48000)
        for position in range(input_tokens)
    ]
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": output_tokens,
        "temperature": 0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.perf_counter()
    first_token_at: float | None = None
    usage: dict[str, Any] = {}
    error = ""
    try:
        async with session.post(
            f"{endpoint}/v1/completions", json=payload
        ) as response:
            if response.status != 200:
                error = f"HTTP {response.status}: {await response.text()}"
            else:
                buffer = b""
                async for chunk in response.content.iter_any():
                    buffer += chunk
                    while b"\n\n" in buffer:
                        raw, buffer = buffer.split(b"\n\n", 1)
                        for line in raw.splitlines():
                            if not line.startswith(b"data: "):
                                continue
                            data = line[6:]
                            if data == b"[DONE]":
                                continue
                            event = json.loads(data)
                            if event.get("usage"):
                                usage = event["usage"]
                            choices = event.get("choices") or []
                            if (
                                first_token_at is None
                                and choices
                                and choices[0].get("text")
                            ):
                                first_token_at = time.perf_counter()
    except Exception as exc:  # benchmark must preserve failures in its result file
        error = repr(exc)

    ended = time.perf_counter()
    prompt_count = int(usage.get("prompt_tokens", 0))
    completion_count = int(usage.get("completion_tokens", 0))
    ok = (
        not error
        and first_token_at is not None
        and prompt_count == input_tokens
        and completion_count == output_tokens
    )
    if not ok and not error:
        error = (
            f"length mismatch: prompt={prompt_count}/{input_tokens}, "
            f"output={completion_count}/{output_tokens}"
        )
    ttft = (first_token_at - started) if first_token_at else math.nan
    e2e = ended - started
    tpot = (
        (e2e - ttft) / max(completion_count - 1, 1)
        if first_token_at is not None
        else math.nan
    )
    return RequestResult(
        concurrency, request_id, endpoint, prompt_count, completion_count,
        ttft, e2e, tpot, ok, error
    )


async def run(args: argparse.Namespace) -> None:
    endpoints = [item.rstrip("/") for item in args.endpoints.split(",")]
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=0)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    detail_path = output_path.with_suffix(".requests.csv")

    summaries: list[dict[str, Any]] = []
    details: list[RequestResult] = []
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        models = {endpoint: await get_model(session, endpoint) for endpoint in endpoints}

        # Compile graphs/kernels and verify each endpoint before timed work.
        warmups = [
            one_request(
                session, endpoint, models[endpoint], 1, -i - 1,
                args.input_tokens, min(args.output_tokens, 128)
            )
            for i, endpoint in enumerate(endpoints)
        ]
        warmup_results = await asyncio.gather(*warmups)
        if not all(result.ok for result in warmup_results):
            raise RuntimeError(f"warmup failed: {warmup_results}")

        next_request_id = 0
        for concurrency in args.concurrency:
            batch_started = time.perf_counter()
            jobs = []
            for _ in range(concurrency):
                endpoint = endpoints[next_request_id % len(endpoints)]
                jobs.append(
                    one_request(
                        session, endpoint, models[endpoint], concurrency,
                        next_request_id, args.input_tokens, args.output_tokens
                    )
                )
                next_request_id += 1
            batch = await asyncio.gather(*jobs)
            wall_s = time.perf_counter() - batch_started
            details.extend(batch)
            successful = [result for result in batch if result.ok]
            total_prompt = sum(result.prompt_tokens for result in successful)
            total_output = sum(result.output_tokens for result in successful)
            summary = {
                "label": args.label,
                "concurrency": concurrency,
                "requests": len(batch),
                "successful": len(successful),
                "batch_wall_s": wall_s,
                "input_tokens_per_s": total_prompt / wall_s,
                "output_tokens_per_s": total_output / wall_s,
                "total_tokens_per_s": (total_prompt + total_output) / wall_s,
                "ttft_mean_s": statistics.mean(r.ttft_s for r in successful)
                if successful else math.nan,
                "ttft_p50_s": percentile([r.ttft_s for r in successful], 0.50),
                "ttft_p95_s": percentile([r.ttft_s for r in successful], 0.95),
                "tpot_mean_ms": 1000 * statistics.mean(r.tpot_s for r in successful)
                if successful else math.nan,
                "e2e_p50_s": percentile([r.e2e_s for r in successful], 0.50),
                "e2e_p95_s": percentile([r.e2e_s for r in successful], 0.95),
            }
            summaries.append(summary)
            print(json.dumps(summary), flush=True)
            output_path.write_text(json.dumps(summaries, indent=2) + "\n")

    with detail_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(details[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(result) for result in details)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoints", required=True,
                        help="comma-separated OpenAI-compatible base URLs")
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--concurrency", type=int, nargs="+",
                        default=[1, 2, 4, 8, 16, 32])
    parser.add_argument("--input-tokens", type=int, default=1024)
    parser.add_argument("--output-tokens", type=int, default=1024)
    parser.add_argument("--timeout", type=float, default=3600)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
