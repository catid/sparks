#!/usr/bin/env python3
"""Streaming OpenAI-chat benchmark using realistic DeepSeek-V4 agent prompts."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import statistics
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from prompts import TERMINAL_TOOL, CalibratedPrompt, build_prompts


DFLASH_METRICS: dict[str, tuple[str, ...]] = {
    "draft_steps": (
        "vllm:spec_decode_num_drafts_total",
        "vllm:spec_decode_num_drafts",
    ),
    "draft_tokens": (
        "vllm:spec_decode_num_draft_tokens_total",
        "vllm:spec_decode_num_draft_tokens",
    ),
    "accepted_tokens": (
        "vllm:spec_decode_num_accepted_tokens_total",
        "vllm:spec_decode_num_accepted_tokens",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return (
        ordered[lower] * (upper - index)
        + ordered[upper] * (index - lower)
    )


def mean_or_none(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def ratio_or_none(numerator: float | None,
                  denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(
                json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
            )
    os.replace(temporary, path)


def request_headers(api_key: str, request_id: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if request_id:
        headers["X-Request-ID"] = request_id
        headers["X-Session-ID"] = request_id
    return headers


def request_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, method="GET", headers=request_headers(api_key)
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def discover_model(endpoint: str, api_key: str, timeout: float) -> str:
    result = request_json(f"{endpoint}/v1/models", api_key, timeout)
    models = result.get("data") or []
    if not models or not isinstance(models[0], dict) or not models[0].get("id"):
        raise RuntimeError(f"{endpoint}/v1/models returned no usable model")
    return str(models[0]["id"])


def parse_prometheus_counters(text: str) -> dict[str, float]:
    """Sum the selected counter series across model/engine labels."""
    totals: dict[str, float] = {}
    wanted = {
        metric_name
        for aliases in DFLASH_METRICS.values()
        for metric_name in aliases
    }
    sample_pattern = re.compile(
        r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{.*\})?"
        r"\s+([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
        r"(?:\s+\d+)?$"
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = sample_pattern.match(line)
        if match is None or match.group(1) not in wanted:
            continue
        totals[match.group(1)] = (
            totals.get(match.group(1), 0.0) + float(match.group(2))
        )
    return totals


def canonical_dflash_counters(parsed: dict[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for canonical_name, aliases in DFLASH_METRICS.items():
        for metric_name in aliases:
            if metric_name in parsed:
                result[canonical_name] = parsed[metric_name]
                break
    return result


def scrape_metrics(endpoint: str, timeout: float) -> dict[str, Any]:
    url = f"{endpoint.rstrip('/')}/metrics"
    started = time.perf_counter()
    captured_at = utc_now()
    try:
        request = urllib.request.Request(
            url, method="GET", headers={"Accept": "text/plain"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        parsed = parse_prometheus_counters(raw)
        return {
            "endpoint": endpoint,
            "url": url,
            "captured_at": captured_at,
            "elapsed_s": time.perf_counter() - started,
            "ok": True,
            "error": None,
            "raw": raw,
            "parsed_metric_names": parsed,
            "dflash_counters": canonical_dflash_counters(parsed),
        }
    except Exception as exc:
        return {
            "endpoint": endpoint,
            "url": url,
            "captured_at": captured_at,
            "elapsed_s": time.perf_counter() - started,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "raw": "",
            "parsed_metric_names": {},
            "dflash_counters": {},
        }


def save_metric_snapshots(
    snapshots: list[dict[str, Any]],
    metrics_dir: Path,
    batch_name: str,
    phase: str,
) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    metrics_dir.mkdir(parents=True, exist_ok=True)
    for index, snapshot in enumerate(snapshots):
        raw_name = f"{batch_name}-{phase}-endpoint{index}.prom"
        raw_path = metrics_dir / raw_name
        raw_path.write_text(snapshot["raw"], encoding="utf-8")
        saved.append({
            key: value
            for key, value in snapshot.items()
            if key != "raw"
        } | {"raw_path": str(raw_path)})
    return saved


def dflash_metric_delta(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    deltas: dict[str, float] = {}
    contributing: dict[str, list[str]] = {}
    missing: dict[str, list[str]] = {}

    for counter_name in DFLASH_METRICS:
        counter_delta = 0.0
        counter_endpoints: list[str] = []
        counter_missing: list[str] = []
        for before_item, after_item in zip(before, after, strict=True):
            endpoint = str(before_item["endpoint"])
            before_value = before_item["dflash_counters"].get(counter_name)
            after_value = after_item["dflash_counters"].get(counter_name)
            if before_value is None or after_value is None:
                counter_missing.append(endpoint)
                continue
            counter_delta += after_value - before_value
            counter_endpoints.append(endpoint)
        if counter_endpoints:
            deltas[counter_name] = counter_delta
            contributing[counter_name] = counter_endpoints
        if counter_missing:
            missing[counter_name] = counter_missing

    draft_steps = deltas.get("draft_steps")
    draft_tokens = deltas.get("draft_tokens")
    accepted_tokens = deltas.get("accepted_tokens")
    has_reset = any(value < 0 for value in deltas.values())
    return {
        "counter_deltas": deltas,
        "contributing_endpoints": contributing,
        "missing_endpoints": missing,
        "counter_reset_detected": has_reset,
        "mean_draft_tokens_per_step": (
            None if has_reset
            else ratio_or_none(draft_tokens, draft_steps)
        ),
        "mean_accepted_tokens_per_step": (
            None if has_reset
            else ratio_or_none(accepted_tokens, draft_steps)
        ),
        "accepted_token_rate": (
            None if has_reset
            else ratio_or_none(accepted_tokens, draft_tokens)
        ),
        "accepted_token_percent": (
            None if has_reset
            else (
                100.0 * accepted_tokens / draft_tokens
                if accepted_tokens is not None
                and draft_tokens is not None
                and draft_tokens > 0
                else None
            )
        ),
    }


def append_fragment(current: str, fragment: Any) -> str:
    return current + fragment if isinstance(fragment, str) else current


class StreamAccumulator:
    def __init__(self) -> None:
        self.role: str | None = None
        self.content = ""
        self.reasoning_by_field = {
            "reasoning_content": "",
            "reasoning": "",
        }
        self.reasoning_fragments: list[dict[str, str]] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.finish_reason: str | None = None
        self.usage: dict[str, Any] = {}
        self.system_fingerprint: str | None = None
        self.response_id: str | None = None
        self.response_model: str | None = None

    def consume(self, event: dict[str, Any]) -> bool:
        """Consume an event and return whether it contains generated output."""
        if isinstance(event.get("id"), str):
            self.response_id = event["id"]
        if isinstance(event.get("model"), str):
            self.response_model = event["model"]
        if isinstance(event.get("system_fingerprint"), str):
            self.system_fingerprint = event["system_fingerprint"]
        if isinstance(event.get("usage"), dict) and event["usage"]:
            self.usage = event["usage"]

        produced_output = False
        for choice in event.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason") is not None:
                self.finish_reason = str(choice["finish_reason"])
            delta = choice.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("role"), str):
                self.role = delta["role"]
            if isinstance(delta.get("content"), str) and delta["content"]:
                self.content += delta["content"]
                produced_output = True
            for field_name in ("reasoning_content", "reasoning"):
                fragment = delta.get(field_name)
                if isinstance(fragment, str) and fragment:
                    self.reasoning_by_field[field_name] += fragment
                    self.reasoning_fragments.append({
                        "field": field_name,
                        "text": fragment,
                    })
                    produced_output = True
            raw_tool_calls = delta.get("tool_calls") or []
            if not isinstance(raw_tool_calls, list):
                raw_tool_calls = [raw_tool_calls]
            for position, raw_call in enumerate(raw_tool_calls):
                if not isinstance(raw_call, dict):
                    continue
                raw_index = raw_call.get("index", position)
                index = raw_index if isinstance(raw_index, int) else position
                call = self.tool_calls.setdefault(index, {
                    "index": index,
                    "id": "",
                    "type": "",
                    "function": {"name": "", "arguments": ""},
                })
                call["id"] = append_fragment(call["id"], raw_call.get("id"))
                call["type"] = append_fragment(
                    call["type"], raw_call.get("type")
                )
                function = raw_call.get("function") or {}
                if isinstance(function, dict):
                    call["function"]["name"] = append_fragment(
                        call["function"]["name"], function.get("name")
                    )
                    call["function"]["arguments"] = append_fragment(
                        call["function"]["arguments"],
                        function.get("arguments"),
                    )
                produced_output = True
        return produced_output

    def message(self) -> dict[str, Any]:
        canonical_reasoning = (
            self.reasoning_by_field["reasoning_content"]
            or self.reasoning_by_field["reasoning"]
        )
        return {
            "role": self.role or "assistant",
            "reasoning": canonical_reasoning,
            "reasoning_content":
                self.reasoning_by_field["reasoning_content"],
            "reasoning_field_values": self.reasoning_by_field,
            "reasoning_fragments": self.reasoning_fragments,
            "content": self.content,
            "tool_calls": [
                self.tool_calls[index] for index in sorted(self.tool_calls)
            ],
        }


def stream_chat_request(
    endpoint: str,
    model: str,
    prompt: CalibratedPrompt,
    concurrency: int,
    wave: int,
    slot: int,
    max_tokens: int,
    temperature: float,
    top_p: float,
    api_key: str,
    timeout: float,
    start_gate: threading.Barrier,
) -> dict[str, Any]:
    request_id = (
        f"dsv4-openclaw-c{concurrency}-w{wave}-s{slot}-"
        f"{uuid.uuid4().hex[:12]}"
    )
    headers = request_headers(api_key, request_id)
    recorded_headers = dict(headers)
    if "Authorization" in recorded_headers:
        recorded_headers["Authorization"] = "Bearer <redacted>"
    payload: dict[str, Any] = {
        "model": model,
        "messages": prompt.messages,
        "tools": [TERMINAL_TOOL],
        "tool_choice": "auto",
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "reasoning_effort": "max",
        "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {
            "thinking": True,
            "reasoning_effort": "max",
        },
    }
    request_body = json.dumps(payload, ensure_ascii=False)
    started_at = ""
    started = 0.0
    response_opened_at: float | None = None
    first_output_at: float | None = None
    ended = 0.0
    status: int | None = None
    response_headers: dict[str, str] = {}
    exact_sse_data: list[dict[str, Any]] = []
    accumulator = StreamAccumulator()
    error: str | None = None

    try:
        start_gate.wait(timeout=30)
        started_at = utc_now()
        started = time.perf_counter()
        request = urllib.request.Request(
            f"{endpoint}/v1/chat/completions",
            data=request_body.encode(),
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_opened_at = time.perf_counter()
            status = response.status
            response_headers = dict(response.headers.items())
            event_data_lines: list[str] = []
            done = False
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
                if line == "":
                    if not event_data_lines:
                        continue
                    data = "\n".join(event_data_lines)
                    event_data_lines = []
                    received_s = time.perf_counter() - started
                    exact_sse_data.append({
                        "received_s": received_s,
                        "data": data,
                    })
                    if data == "[DONE]":
                        done = True
                        continue
                    event = json.loads(data)
                    if (
                        accumulator.consume(event)
                        and first_output_at is None
                    ):
                        first_output_at = time.perf_counter()
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    value = line[5:]
                    if value.startswith(" "):
                        value = value[1:]
                    event_data_lines.append(value)
            if event_data_lines:
                data = "\n".join(event_data_lines)
                received_s = time.perf_counter() - started
                exact_sse_data.append({
                    "received_s": received_s,
                    "data": data,
                })
                if data != "[DONE]":
                    event = json.loads(data)
                    if (
                        accumulator.consume(event)
                        and first_output_at is None
                    ):
                        first_output_at = time.perf_counter()
                else:
                    done = True
            if not done:
                error = "stream ended without data: [DONE]"
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
        error = f"HTTP {exc.code}: {body}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        ended = time.perf_counter()

    e2e_s = ended - started if started else 0.0
    ttfb_s = (
        response_opened_at - started
        if started and response_opened_at is not None
        else None
    )
    ttft_s = (
        first_output_at - started
        if started and first_output_at is not None
        else None
    )
    usage = accumulator.usage
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    usage_complete = (
        isinstance(prompt_tokens, int)
        and not isinstance(prompt_tokens, bool)
        and isinstance(completion_tokens, int)
        and not isinstance(completion_tokens, bool)
    )
    if error is None and first_output_at is None:
        error = "stream contained no generated reasoning, content, or tool call"
    if error is None and accumulator.finish_reason is None:
        error = "stream contained no finish_reason"
    if error is None and not usage_complete:
        error = "stream contained no complete token usage"

    return {
        "schema_version": 1,
        "request_id": request_id,
        "concurrency": concurrency,
        "wave": wave,
        "slot": slot,
        "variant": prompt.name,
        "endpoint": endpoint,
        "started_at": started_at,
        "request": {
            "headers": recorded_headers,
            "authorization_header_redacted": "Authorization" in headers,
            "exact_body_json": request_body,
            "payload": payload,
            "calibration": {
                "method": prompt.calibration_method,
                "expected_input_tokens": prompt.input_tokens,
                "target_input_tokens": prompt.target_input_tokens,
                "token_delta": prompt.token_delta,
                "rendered_prompt_sha256":
                    prompt.rendered_prompt_sha256,
            },
        },
        "response": {
            "http_status": status,
            "headers": response_headers,
            "id": accumulator.response_id,
            "model": accumulator.response_model,
            "system_fingerprint": accumulator.system_fingerprint,
            "exact_sse_data": exact_sse_data,
            "message": accumulator.message(),
            "finish_reason": accumulator.finish_reason,
            "usage": usage,
        },
        "timing": {
            "ttfb_s": ttfb_s,
            "ttft_s": ttft_s,
            "e2e_s": e2e_s,
            "output_tokens_per_s": (
                completion_tokens / e2e_s
                if usage_complete and e2e_s > 0
                else None
            ),
        },
        "observed_input_token_delta": (
            prompt_tokens - prompt.input_tokens
            if usage_complete and prompt.input_tokens is not None
            else None
        ),
        "ok": error is None,
        "error": error,
    }


def batch_summary(
    label: str,
    concurrency: int,
    wave: int,
    max_tokens: int,
    wall_s: float,
    results: list[dict[str, Any]],
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    saved_before: list[dict[str, Any]],
    saved_after: list[dict[str, Any]],
) -> dict[str, Any]:
    successful = [item for item in results if item["ok"]]
    usages = [item["response"]["usage"] for item in successful]
    total_prompt_tokens = sum(
        int(usage["prompt_tokens"]) for usage in usages
    )
    total_completion_tokens = sum(
        int(usage["completion_tokens"]) for usage in usages
    )
    completion_token_counts = [
        int(usage["completion_tokens"]) for usage in usages
    ]
    ttft_values = [
        float(item["timing"]["ttft_s"])
        for item in successful if item["timing"]["ttft_s"] is not None
    ]
    e2e_values = [
        float(item["timing"]["e2e_s"]) for item in successful
    ]
    finish_reasons = Counter(
        str(item["response"]["finish_reason"]) for item in successful
    )
    return {
        "label": label,
        "concurrency": concurrency,
        "wave": wave,
        "requests": len(results),
        "successful": len(successful),
        "failed": len(results) - len(successful),
        "batch_wall_s": wall_s,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "completion_tokens_per_request": {
            "mean": mean_or_none(
                [float(value) for value in completion_token_counts]
            ),
            "min": (
                min(completion_token_counts)
                if completion_token_counts else None
            ),
            "max": (
                max(completion_token_counts)
                if completion_token_counts else None
            ),
        },
        "requests_at_output_cap": sum(
            value >= max_tokens for value in completion_token_counts
        ),
        "variant_counts": dict(sorted(Counter(
            item["variant"] for item in successful
        ).items())),
        "aggregate_prompt_tokens_per_s": (
            total_prompt_tokens / wall_s if wall_s > 0 else None
        ),
        "aggregate_output_tokens_per_s": (
            total_completion_tokens / wall_s if wall_s > 0 else None
        ),
        "latency": {
            "ttft_mean_s": mean_or_none(ttft_values),
            "ttft_p50_s": percentile(ttft_values, 0.50),
            "ttft_p95_s": percentile(ttft_values, 0.95),
            "e2e_mean_s": mean_or_none(e2e_values),
            "e2e_p50_s": percentile(e2e_values, 0.50),
            "e2e_p95_s": percentile(e2e_values, 0.95),
        },
        "finish_reasons": dict(sorted(finish_reasons.items())),
        "metrics": {
            "before": saved_before,
            "after": saved_after,
            "dflash": dflash_metric_delta(before, after),
        },
        "request_ids": [item["request_id"] for item in results],
    }


def concurrency_aggregate(
    concurrency: int,
    summaries: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = [
        item for item in summaries if item["concurrency"] == concurrency
    ]
    total_wall = sum(float(item["batch_wall_s"]) for item in selected)
    total_output = sum(int(item["completion_tokens"]) for item in selected)
    total_prompt = sum(int(item["prompt_tokens"]) for item in selected)
    return {
        "concurrency": concurrency,
        "waves": len(selected),
        "requests": sum(int(item["requests"]) for item in selected),
        "successful": sum(int(item["successful"]) for item in selected),
        "batch_wall_s": total_wall,
        "prompt_tokens": total_prompt,
        "completion_tokens": total_output,
        "requests_at_output_cap": sum(
            int(item["requests_at_output_cap"]) for item in selected
        ),
        "aggregate_prompt_tokens_per_s": (
            total_prompt / total_wall if total_wall > 0 else None
        ),
        "aggregate_output_tokens_per_s": (
            total_output / total_wall if total_wall > 0 else None
        ),
        "batch_e2e_p50_s": percentile(
            [float(item["batch_wall_s"]) for item in selected], 0.50
        ),
        "batch_e2e_p95_s": percentile(
            [float(item["batch_wall_s"]) for item in selected], 0.95
        ),
    }


def run(args: argparse.Namespace) -> None:
    endpoints = [
        item.rstrip("/") for item in args.endpoints.split(",") if item.strip()
    ]
    if not endpoints:
        raise ValueError("--endpoints contains no endpoint")
    metric_endpoints = (
        [
            item.rstrip("/")
            for item in args.metrics_endpoints.split(",")
            if item.strip()
        ]
        if args.metrics_endpoints
        else endpoints
    )
    prompts = build_prompts(
        args.model_path,
        target_input_tokens=args.target_input_tokens,
        tolerance=args.prompt_tolerance,
        calibrate=not args.no_prompt_calibration,
    )
    if args.prompt_variant:
        prompts = [
            prompt for prompt in prompts
            if prompt.name == args.prompt_variant
        ]
        if not prompts:
            raise ValueError(
                f"unknown --prompt-variant {args.prompt_variant!r}"
            )
    models = {
        endpoint: (
            args.model
            or discover_model(endpoint, args.api_key, args.request_timeout)
        )
        for endpoint in endpoints
    }

    output_path = args.output
    requests_path = output_path.with_suffix(".requests.jsonl")
    metrics_dir = output_path.with_suffix(".metrics")
    run_started = utc_now()
    request_results: list[dict[str, Any]] = []
    batch_summaries: list[dict[str, Any]] = []
    maximum_workers = max(args.concurrency)
    global_request_index = 0

    result_document: dict[str, Any] = {
        "schema_version": 1,
        "label": args.label,
        "started_at": run_started,
        "completed_at": None,
        "configuration": {
            "endpoints": endpoints,
            "metrics_endpoints": metric_endpoints,
            "models": models,
            "concurrency": args.concurrency,
            "waves": args.waves,
            "max_tokens": args.max_tokens,
            "target_input_tokens": args.target_input_tokens,
            "prompt_tolerance": args.prompt_tolerance,
            "prompt_variant": args.prompt_variant or None,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "thinking": True,
            "reasoning_effort": "max",
            "stream": True,
            "include_usage": True,
            "tool_choice": "auto",
        },
        "prompts": [
            {
                "name": prompt.name,
                "messages": prompt.messages,
                "input_tokens": prompt.input_tokens,
                "target_input_tokens": prompt.target_input_tokens,
                "token_delta": prompt.token_delta,
                "rendered_prompt_sha256":
                    prompt.rendered_prompt_sha256,
                "calibration_method": prompt.calibration_method,
            }
            for prompt in prompts
        ],
        "batches": batch_summaries,
        "concurrency_aggregates": [],
        "artifacts": {
            "requests_jsonl": str(requests_path),
            "metrics_directory": str(metrics_dir),
        },
    }
    write_json(output_path, result_document)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=maximum_workers,
        thread_name_prefix="dsv4-bench",
    ) as executor:
        for concurrency in args.concurrency:
            for wave in range(1, args.waves + 1):
                batch_name = f"c{concurrency}-w{wave}"
                before = [
                    scrape_metrics(endpoint, args.metrics_timeout)
                    for endpoint in metric_endpoints
                ]
                saved_before = save_metric_snapshots(
                    before, metrics_dir, batch_name, "before"
                )

                # Main is an extra barrier participant so the measured batch
                # interval begins before any request is released.
                gate = threading.Barrier(concurrency + 1)
                futures: list[concurrent.futures.Future[dict[str, Any]]] = []
                for slot in range(concurrency):
                    endpoint = endpoints[
                        global_request_index % len(endpoints)
                    ]
                    prompt = prompts[
                        global_request_index % len(prompts)
                    ]
                    futures.append(executor.submit(
                        stream_chat_request,
                        endpoint,
                        models[endpoint],
                        prompt,
                        concurrency,
                        wave,
                        slot,
                        args.max_tokens,
                        args.temperature,
                        args.top_p,
                        args.api_key,
                        args.request_timeout,
                        gate,
                    ))
                    global_request_index += 1

                batch_started = time.perf_counter()
                gate.wait(timeout=30)
                results = [future.result() for future in futures]
                wall_s = time.perf_counter() - batch_started
                after = [
                    scrape_metrics(endpoint, args.metrics_timeout)
                    for endpoint in metric_endpoints
                ]
                saved_after = save_metric_snapshots(
                    after, metrics_dir, batch_name, "after"
                )
                summary = batch_summary(
                    args.label,
                    concurrency,
                    wave,
                    args.max_tokens,
                    wall_s,
                    results,
                    before,
                    after,
                    saved_before,
                    saved_after,
                )
                request_results.extend(results)
                batch_summaries.append(summary)
                result_document["batches"] = batch_summaries
                result_document["concurrency_aggregates"] = [
                    concurrency_aggregate(value, batch_summaries)
                    for value in args.concurrency
                    if any(
                        item["concurrency"] == value
                        for item in batch_summaries
                    )
                ]
                write_jsonl(requests_path, request_results)
                write_json(output_path, result_document)
                print(json.dumps({
                    "concurrency": concurrency,
                    "wave": wave,
                    "successful": summary["successful"],
                    "requests": summary["requests"],
                    "aggregate_output_tokens_per_s":
                        summary["aggregate_output_tokens_per_s"],
                    "ttft_p50_s": summary["latency"]["ttft_p50_s"],
                    "e2e_p50_s": summary["latency"]["e2e_p50_s"],
                    "dflash": summary["metrics"]["dflash"],
                }, ensure_ascii=False), flush=True)

    result_document["completed_at"] = utc_now()
    result_document["concurrency_aggregates"] = [
        concurrency_aggregate(value, batch_summaries)
        for value in args.concurrency
    ]
    write_json(output_path, result_document)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoints",
        default="http://127.0.0.1:8000",
        help=(
            "Comma-separated OpenAI-compatible base endpoints. Use the router "
            "alone, or direct endpoints for explicit round-robin distribution."
        ),
    )
    parser.add_argument(
        "--metrics-endpoints",
        default="",
        help=(
            "Comma-separated vLLM endpoints to scrape around each batch. "
            "Defaults to --endpoints; when benchmarking a router, name both "
            "direct backend endpoints here."
        ),
    )
    parser.add_argument("--api-key", default="")
    parser.add_argument(
        "--model",
        default="",
        help="Served model name; empty discovers /v1/models on each endpoint.",
    )
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--model-path",
        type=Path,
        default=Path("/home/catid/models/DeepSeek-V4-Flash-NVFP4"),
        help="Local checkpoint containing tokenizer.json and encoding/.",
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int,
        nargs="+",
        default=[1, 2, 4, 8, 16, 32],
    )
    parser.add_argument("--waves", type=positive_int, default=1)
    parser.add_argument(
        "--prompt-variant",
        default="",
        help=(
            "Run only one calibrated prompt variant. This keeps the workload "
            "identical across concurrency levels; empty cycles all variants."
        ),
    )
    parser.add_argument("--target-input-tokens", type=positive_int, default=1024)
    parser.add_argument("--prompt-tolerance", type=int, default=4)
    parser.add_argument("--max-tokens", type=positive_int, default=1024)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=1.0)
    parser.add_argument("--request-timeout", type=float, default=7200)
    parser.add_argument("--metrics-timeout", type=float, default=15)
    parser.add_argument(
        "--no-prompt-calibration",
        action="store_true",
        help="Skip local DeepSeek tokenizer calibration (not recommended).",
    )
    args = parser.parse_args()
    if args.prompt_tolerance < 0:
        parser.error("--prompt-tolerance cannot be negative")
    if len(set(args.concurrency)) != len(args.concurrency):
        parser.error("--concurrency values must be unique")
    run(args)


if __name__ == "__main__":
    main()
