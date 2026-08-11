#!/usr/bin/env python3
"""Auditable OpenAI-compatible streaming benchmark for DeepSeek V4.

This is intentionally a single-turn harness.  It advertises realistic
OpenClaw-style filesystem and shell tools, but it only records model output:
tool calls are never executed.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import aiohttp
except ModuleNotFoundError:  # Dry-run validation intentionally has no network deps.
    aiohttp = None  # type: ignore[assignment]


DEFAULT_CONCURRENCY = (1, 2, 4, 8, 16, 32)
DEFAULT_PROMPT_TOKENS = 1024
DEFAULT_OUTPUT_TOKENS = 1024

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": (
                "Run a shell command in the repository and return stdout, "
                "stderr, and the exit code. Use read-only inspection before "
                "changing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 120000,
                    },
                },
                "required": ["command", "workdir"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a UTF-8 repository file without modifying it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": (
                "Replace one exact text occurrence in a repository file. "
                "Fails if old_text is absent or ambiguous."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
                "additionalProperties": False,
            },
        },
    },
]

_PAD_VOCABULARY = (
    "repository module worker queue cancellation timeout cleanup lifecycle "
    "pytest regression invariant atomic rollback logging metrics fixture "
    "asyncio signal shutdown exception ownership task result ordering lock "
    "deadline process resource pending idempotent review validation"
).split()

_SPEC_METRICS = (
    "vllm:spec_decode_num_drafts_total",
    "vllm:spec_decode_num_draft_tokens_total",
    "vllm:spec_decode_num_accepted_tokens_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: Iterable[float], quantile: float) -> float | None:
    finite = sorted(value for value in values if math.isfinite(value))
    if not finite:
        return None
    position = (len(finite) - 1) * quantile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return finite[low]
    return finite[low] * (high - position) + finite[high] * (position - low)


def mean_or_none(values: Iterable[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    return statistics.mean(finite) if finite else None


def json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def csv_write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def normalize_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint = endpoint[:-3]
    return endpoint


def safe_headers(api_key: str | None, request_id: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if request_id:
        headers["X-Request-ID"] = request_id
        headers["X-Session-ID"] = request_id
    return headers


def persisted_headers(request_id: str) -> dict[str, str]:
    """Headers safe to persist; authorization is deliberately excluded."""
    return {
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
        "X-Session-ID": request_id,
    }


def padding_text(case_index: int, word_count: int) -> str:
    if word_count <= 0:
        return ""
    words = [
        _PAD_VOCABULARY[
            (position * 7 + case_index * 11) % len(_PAD_VOCABULARY)
        ]
        for position in range(word_count)
    ]
    return (
        "\n\nIssue-search index terms supplied by the maintainer; use them only "
        "to guide inspection, not as proof:\n" + " ".join(words)
    )


def build_messages(case_index: int, pad_words: int) -> list[dict[str, str]]:
    digest = hashlib.sha256(f"deepseek-v4-case-{case_index}".encode()).hexdigest()[:12]
    system = f"""You are a coding agent running inside OpenClaw on a Linux host.
Session: local-benchmark-{case_index:02d}-{digest}. Work from evidence and make
the smallest safe change. Inspect relevant files before editing, run focused
tests before broader tests, and report commands and results precisely. Tool
responses are authoritative: never invent output. Stay within /workspace/queuepilot.
Do not access secrets, the network, services, or paths outside the workspace.
If a command could be destructive, explain and choose a safer inspection."""

    user = """Fix the cancellation race in this small Python repository and
leave it ready for review. Begin by using the tools to inspect the real files
and reproduce the focused failure; do not merely propose commands.

The reported implementation resembles:

```python
async def stop(self) -> None:
    self._closing = True
    for task in self._workers:
        task.cancel()
    self._workers.clear()
```

The failing test creates four workers, cancels `stop()` while worker cleanup is
in progress, calls `stop()` again, and then asserts there are no live worker
tasks. CI reports `Task was destroyed but it is pending!`; a separate test
requires repeated `stop()` calls to remain safe. Python is 3.12, dependencies
may not be added, and the public API must not change.

Acceptance criteria:

1. Establish the exact current code and focused failure before editing.
2. Preserve cancellation semantics: caller cancellation must propagate, while
   worker cleanup still reaches a stable state.
3. Keep shutdown idempotent under two concurrent callers.
4. Add or refine a regression test only if existing coverage does not prove
   the race.
5. Run the focused test, then the relevant test file. Avoid unrelated edits.
6. Finish with a concise account of evidence, patch, tests, and residual risk.

Treat the snapshot and CI excerpt as a lead, not a substitute for inspection."""
    user += padding_text(case_index, pad_words)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def chat_template_kwargs() -> dict[str, Any]:
    # Keep this identical for /tokenize and /v1/chat/completions so calibrated
    # token counts describe the request that is actually benchmarked.
    return {
        "enable_thinking": True,
        "thinking": True,
        "reasoning_effort": "max",
    }


def build_chat_payload(
    model: str,
    messages: list[dict[str, str]],
    output_tokens: int,
    seed: int,
    force_exact_length: bool = True,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "temperature": 1.0,
        "top_p": 1.0,
        "reasoning_effort": "max",
        "chat_template_kwargs": chat_template_kwargs(),
        "max_tokens": output_tokens,
        "stream": True,
        "stream_options": {
            "include_usage": True,
            "continuous_usage_stats": False,
        },
        "seed": seed,
    }
    if force_exact_length:
        # These are vLLM extensions. Together they prevent an early EOS/tool
        # boundary from making concurrency rows incomparable.
        payload["min_tokens"] = output_tokens
        payload["ignore_eos"] = True
    return payload


@dataclass(frozen=True)
class PromptCase:
    case_index: int
    pad_words: int
    token_count: int
    messages: list[dict[str, str]]


class TokenizerClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        model: str,
        headers: dict[str, str],
    ) -> None:
        self.session = session
        self.endpoint = endpoint
        self.model = model
        self.headers = headers
        self.path: str | None = None
        self.cache: dict[tuple[int, int], int] = {}

    async def discover(self) -> None:
        messages = build_messages(0, 0)
        errors: list[str] = []
        for candidate in ("/tokenize", "/v1/tokenize"):
            try:
                count = await self._post(candidate, messages)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            self.path = candidate
            self.cache[(0, 0)] = count
            return
        raise RuntimeError(
            "No chat-aware tokenize endpoint succeeded. Tried /tokenize and "
            f"/v1/tokenize: {'; '.join(errors)}"
        )

    async def _post(
        self, path: str, messages: list[dict[str, str]]
    ) -> int:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "add_generation_prompt": True,
            "chat_template_kwargs": chat_template_kwargs(),
        }
        async with self.session.post(
            f"{self.endpoint}{path}", json=body, headers=self.headers
        ) as response:
            text = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"{path} returned HTTP {response.status}: {text[:1000]}"
                )
            try:
                result = json.loads(text)
                count = int(result["count"])
            except (ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"{path} returned an invalid tokenize response: {text[:1000]}"
                ) from exc
            tokens = result.get("tokens")
            if isinstance(tokens, list) and len(tokens) != count:
                raise RuntimeError(
                    f"{path} count={count} but returned {len(tokens)} token IDs"
                )
            return count

    async def count(self, case_index: int, pad_words: int) -> int:
        key = (case_index, pad_words)
        if key in self.cache:
            return self.cache[key]
        if self.path is None:
            raise RuntimeError("TokenizerClient.discover() was not called")
        count = await self._post(
            self.path, build_messages(case_index, pad_words)
        )
        self.cache[key] = count
        return count


async def calibrate_prompt(
    tokenizer: TokenizerClient,
    case_index: int,
    target: int,
    tolerance: int,
    maximum_pad_words: int,
) -> PromptCase:
    base_count = await tokenizer.count(case_index, 0)
    if base_count > target + tolerance:
        raise RuntimeError(
            f"case {case_index}: unpadded prompt is {base_count} tokens, above "
            f"target {target} +/- {tolerance}"
        )

    high_count = await tokenizer.count(case_index, maximum_pad_words)
    if high_count < target - tolerance:
        raise RuntimeError(
            f"case {case_index}: even {maximum_pad_words} pad words produce "
            f"only {high_count} tokens"
        )

    low = 0
    high = maximum_pad_words
    candidates: dict[int, int] = {0: base_count, maximum_pad_words: high_count}
    while low <= high:
        middle = (low + high) // 2
        count = await tokenizer.count(case_index, middle)
        candidates[middle] = count
        if count < target:
            low = middle + 1
        elif count > target:
            high = middle - 1
        else:
            break

    pivot = min(candidates, key=lambda words: abs(candidates[words] - target))
    for words in range(max(0, pivot - 12), min(maximum_pad_words, pivot + 12) + 1):
        candidates[words] = await tokenizer.count(case_index, words)
    best_words = min(
        candidates,
        key=lambda words: (
            abs(candidates[words] - target),
            candidates[words] > target,
            words,
        ),
    )
    token_count = candidates[best_words]
    if abs(token_count - target) > tolerance:
        raise RuntimeError(
            f"case {case_index}: closest prompt is {token_count} tokens with "
            f"{best_words} pad words; target is {target} +/- {tolerance}"
        )
    return PromptCase(
        case_index=case_index,
        pad_words=best_words,
        token_count=token_count,
        messages=build_messages(case_index, best_words),
    )


class SSEAccumulator:
    """Incrementally parse SSE while retaining the byte-exact response."""

    _separator = re.compile(br"\r?\n\r?\n")

    def __init__(self, started: float) -> None:
        self.started = started
        self.raw = bytearray()
        self.buffer = bytearray()
        self.events: list[dict[str, Any]] = []
        self.parse_errors: list[str] = []
        self.first_output_at: float | None = None
        self.last_output_at: float | None = None
        self.usage: dict[str, Any] = {}
        self.finish_reasons: list[str] = []
        self.content_parts: list[str] = []
        self.reasoning_parts: list[str] = []
        self.tool_calls: dict[int, dict[str, Any]] = {}
        self.saw_done = False

    def feed(self, chunk: bytes, received_at: float) -> None:
        self.raw.extend(chunk)
        self.buffer.extend(chunk)
        while True:
            match = self._separator.search(self.buffer)
            if match is None:
                break
            block = bytes(self.buffer[: match.start()])
            del self.buffer[: match.end()]
            self._consume_block(block, received_at)

    def finish(self, received_at: float) -> None:
        if self.buffer.strip():
            self._consume_block(bytes(self.buffer), received_at)
        self.buffer.clear()

    def _consume_block(self, block: bytes, received_at: float) -> None:
        data_lines: list[bytes] = []
        for line in block.splitlines():
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip(b" "))
        if not data_lines:
            return
        data = b"\n".join(data_lines)
        offset = received_at - self.started
        if data.strip() == b"[DONE]":
            self.saw_done = True
            self.events.append(
                {"received_offset_s": offset, "data": "[DONE]", "parsed": None}
            )
            return
        decoded = data.decode("utf-8", errors="replace")
        try:
            event = json.loads(decoded)
        except json.JSONDecodeError as exc:
            error = f"invalid JSON SSE event at {offset:.6f}s: {exc}"
            self.parse_errors.append(error)
            self.events.append(
                {
                    "received_offset_s": offset,
                    "data": decoded,
                    "parsed": None,
                    "parse_error": str(exc),
                }
            )
            return
        self.events.append(
            {"received_offset_s": offset, "data": decoded, "parsed": event}
        )
        if isinstance(event.get("usage"), dict):
            self.usage = event["usage"]
        for choice in event.get("choices") or []:
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                self.finish_reasons.append(str(finish_reason))
            delta = choice.get("delta") or {}
            has_output = self._consume_delta(delta)
            if has_output:
                if self.first_output_at is None:
                    self.first_output_at = received_at
                self.last_output_at = received_at

    def _consume_delta(self, delta: dict[str, Any]) -> bool:
        has_output = False
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.content_parts.append(content)
            has_output = True
        reasoning = delta.get("reasoning")
        if reasoning is None:
            reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self.reasoning_parts.append(reasoning)
            has_output = True
        for item in delta.get("tool_calls") or []:
            index = int(item.get("index", 0))
            target = self.tool_calls.setdefault(
                index,
                {
                    "index": index,
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if item.get("id"):
                target["id"] += str(item["id"])
                has_output = True
            if item.get("type"):
                target["type"] = item["type"]
            function = item.get("function") or {}
            if function.get("name"):
                target["function"]["name"] += str(function["name"])
                has_output = True
            if function.get("arguments"):
                target["function"]["arguments"] += str(function["arguments"])
                has_output = True
        return has_output

    def reconstructed(self) -> dict[str, Any]:
        return {
            "reasoning": "".join(self.reasoning_parts),
            "content": "".join(self.content_parts),
            "tool_calls": [self.tool_calls[key] for key in sorted(self.tool_calls)],
        }


@dataclass
class RequestResult:
    stage: str
    batch_size: int
    repeat: int
    slot: int
    case_index: int
    request_id: str
    http_status: int | None
    calibrated_prompt_tokens: int
    reported_prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None
    ttft_s: float
    e2e_s: float
    stream_decode_s: float
    output_tokens_per_s_e2e: float
    output_tokens_per_s_after_first: float
    finish_reason: str
    saw_done: bool
    sse_events: int
    ok: bool
    error: str
    request_path: str
    raw_response_path: str
    events_path: str
    parsed_response_path: str


@dataclass
class RequestCapture:
    result: RequestResult
    raw_response: bytes
    events: list[dict[str, Any]]
    response_record: dict[str, Any]


def finite_rate(numerator: int, denominator: float) -> float:
    if numerator <= 0 or denominator <= 0 or not math.isfinite(denominator):
        return math.nan
    return numerator / denominator


def completion_length_error(
    completion_tokens: int,
    expected_output_tokens: int | None,
) -> str | None:
    """Return an exact-length error, or skip it for natural-stop requests."""
    if (
        expected_output_tokens is None
        or completion_tokens == expected_output_tokens
    ):
        return None
    return (
        f"completion length {completion_tokens}, "
        f"expected {expected_output_tokens}"
    )


async def stream_one(
    session: aiohttp.ClientSession,
    endpoint: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    metadata: dict[str, Any],
    relative_paths: dict[str, str],
    expected_output_tokens: int | None,
    prompt_tolerance: int,
) -> RequestCapture:
    started = time.perf_counter()
    accumulator = SSEAccumulator(started)
    http_status: int | None = None
    response_headers: dict[str, str] = {}
    error = ""
    try:
        async with session.post(
            f"{endpoint}/v1/chat/completions",
            json=payload,
            headers=headers,
        ) as response:
            http_status = response.status
            response_headers = dict(response.headers)
            async for chunk in response.content.iter_any():
                accumulator.feed(chunk, time.perf_counter())
            accumulator.finish(time.perf_counter())
            if response.status != 200:
                error = (
                    f"HTTP {response.status}: "
                    f"{bytes(accumulator.raw[:4000]).decode(errors='replace')}"
                )
    except Exception as exc:  # Preserve failures as benchmark artifacts.
        error = f"{type(exc).__name__}: {exc}"
    ended = time.perf_counter()

    usage = accumulator.usage
    reported_prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    completion_details = usage.get("completion_tokens_details") or {}
    reasoning_value = completion_details.get("reasoning_tokens")
    reasoning_tokens = (
        int(reasoning_value) if reasoning_value is not None else None
    )
    ttft = (
        accumulator.first_output_at - started
        if accumulator.first_output_at is not None
        else math.nan
    )
    e2e = ended - started
    after_first = (
        ended - accumulator.first_output_at
        if accumulator.first_output_at is not None
        else math.nan
    )
    stream_decode = (
        accumulator.last_output_at - accumulator.first_output_at
        if accumulator.first_output_at is not None
        and accumulator.last_output_at is not None
        else math.nan
    )

    validation_errors: list[str] = []
    if error:
        validation_errors.append(error)
    if http_status != 200:
        validation_errors.append(f"expected HTTP 200, got {http_status}")
    if accumulator.first_output_at is None:
        validation_errors.append("stream contained no non-empty output delta")
    if length_error := completion_length_error(
        completion, expected_output_tokens
    ):
        validation_errors.append(length_error)
    calibrated_prompt = int(metadata["calibrated_prompt_tokens"])
    if abs(reported_prompt - calibrated_prompt) > prompt_tolerance:
        validation_errors.append(
            f"server prompt usage {reported_prompt}, calibrated "
            f"{calibrated_prompt} (+/- {prompt_tolerance})"
        )
    if accumulator.parse_errors:
        validation_errors.extend(accumulator.parse_errors)

    finish_reason = (
        accumulator.finish_reasons[-1] if accumulator.finish_reasons else ""
    )
    result = RequestResult(
        stage=str(metadata["stage"]),
        batch_size=int(metadata["batch_size"]),
        repeat=int(metadata["repeat"]),
        slot=int(metadata["slot"]),
        case_index=int(metadata["case_index"]),
        request_id=str(metadata["request_id"]),
        http_status=http_status,
        calibrated_prompt_tokens=calibrated_prompt,
        reported_prompt_tokens=reported_prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning_tokens,
        ttft_s=ttft,
        e2e_s=e2e,
        stream_decode_s=stream_decode,
        output_tokens_per_s_e2e=finite_rate(completion, e2e),
        # The token that establishes TTFT is already present at the beginning
        # of this window, so only the remaining N-1 tokens belong in its rate.
        output_tokens_per_s_after_first=finite_rate(
            max(completion - 1, 0), after_first
        ),
        finish_reason=finish_reason,
        saw_done=accumulator.saw_done,
        sse_events=len(accumulator.events),
        ok=not validation_errors,
        error="; ".join(validation_errors),
        request_path=relative_paths["request"],
        raw_response_path=relative_paths["raw"],
        events_path=relative_paths["events"],
        parsed_response_path=relative_paths["parsed"],
    )
    response_record = {
        "request": metadata,
        "http_status": http_status,
        "response_headers": response_headers,
        "timing": {
            "started_perf_counter": started,
            "ended_perf_counter": ended,
            "ttft_s": None if not math.isfinite(ttft) else ttft,
            "e2e_s": e2e,
            "stream_decode_s": (
                None if not math.isfinite(stream_decode) else stream_decode
            ),
        },
        "usage": usage,
        "finish_reasons": accumulator.finish_reasons,
        "saw_done": accumulator.saw_done,
        "parse_errors": accumulator.parse_errors,
        "reconstructed": accumulator.reconstructed(),
        "validation": {"ok": result.ok, "error": result.error},
    }
    return RequestCapture(
        result=result,
        raw_response=bytes(accumulator.raw),
        events=accumulator.events,
        response_record=response_record,
    )


def artifact_stem(
    stage: str, batch_size: int, repeat: int, slot: int, request_id: str
) -> str:
    short_id = request_id.rsplit("-", 1)[-1][:8]
    return (
        f"{stage}-c{batch_size:02d}-r{repeat:02d}-"
        f"s{slot:02d}-{short_id}"
    )


def prepare_request_artifacts(
    output_dir: Path,
    stage: str,
    batch_size: int,
    repeat: int,
    slot: int,
    prompt: PromptCase,
    model: str,
    output_tokens: int,
    seed: int,
    force_exact_length: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    request_id = f"dsv4-bench-{uuid.uuid4()}"
    payload = build_chat_payload(
        model=model,
        messages=prompt.messages,
        output_tokens=output_tokens,
        seed=seed,
        force_exact_length=force_exact_length,
    )
    stem = artifact_stem(stage, batch_size, repeat, slot, request_id)
    relative = {
        "request": f"requests/{stem}.request.json",
        "raw": f"responses/{stem}.response.sse",
        "events": f"responses/{stem}.events.jsonl",
        "parsed": f"responses/{stem}.response.json",
    }
    metadata = {
        "stage": stage,
        "batch_size": batch_size,
        "repeat": repeat,
        "slot": slot,
        "case_index": prompt.case_index,
        "request_id": request_id,
        "calibrated_prompt_tokens": prompt.token_count,
        "pad_words": prompt.pad_words,
        "expected_output_tokens": output_tokens,
        "force_exact_length": force_exact_length,
        "seed": seed,
    }
    json_write(
        output_dir / relative["request"],
        {
            "method": "POST",
            "url": "/v1/chat/completions",
            "headers": persisted_headers(request_id),
            "metadata": metadata,
            "body": payload,
        },
    )
    return payload, metadata, relative


def persist_capture(output_dir: Path, capture: RequestCapture) -> None:
    result = capture.result
    raw_path = output_dir / result.raw_response_path
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(capture.raw_response)
    events_path = output_dir / result.events_path
    with events_path.open("w", encoding="utf-8") as handle:
        for event in capture.events:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    json_write(output_dir / result.parsed_response_path, capture.response_record)


async def run_wave(
    session: aiohttp.ClientSession,
    endpoint: str,
    api_key: str | None,
    output_dir: Path,
    prompts: list[PromptCase],
    model: str,
    stage: str,
    batch_size: int,
    repeat: int,
    output_tokens: int,
    seed: int,
    force_exact_length: bool,
    prompt_tolerance: int,
) -> tuple[list[RequestResult], float]:
    prepared = [
        prepare_request_artifacts(
            output_dir=output_dir,
            stage=stage,
            batch_size=batch_size,
            repeat=repeat,
            slot=slot,
            prompt=prompts[slot],
            model=model,
            output_tokens=output_tokens,
            seed=seed + prompts[slot].case_index,
            force_exact_length=force_exact_length,
        )
        for slot in range(batch_size)
    ]
    started = time.perf_counter()
    captures = await asyncio.gather(
        *[
            stream_one(
                session=session,
                endpoint=endpoint,
                headers=safe_headers(api_key, metadata["request_id"]),
                payload=payload,
                metadata=metadata,
                relative_paths=relative,
                expected_output_tokens=(
                    output_tokens if force_exact_length else None
                ),
                prompt_tolerance=prompt_tolerance,
            )
            for payload, metadata, relative in prepared
        ]
    )
    wall_s = time.perf_counter() - started
    # Persisting can add observable disk time, so it happens after the measured
    # network/inference wave has ended.
    for capture in captures:
        persist_capture(output_dir, capture)
    return [capture.result for capture in captures], wall_s


def summarize_wave(
    label: str,
    batch_size: int,
    repeat: int,
    wall_s: float,
    results: list[RequestResult],
) -> dict[str, Any]:
    good = [item for item in results if item.ok]
    prompt_total = sum(item.reported_prompt_tokens for item in good)
    output_total = sum(item.completion_tokens for item in good)
    aggregate_tps = output_total / wall_s if wall_s > 0 else math.nan
    finish_counts = Counter(item.finish_reason or "<missing>" for item in results)
    return {
        "label": label,
        "batch_size": batch_size,
        "repeat": repeat,
        "requests": len(results),
        "successful": len(good),
        "batch_wall_s": wall_s,
        "reported_prompt_tokens_total": prompt_total,
        "completion_tokens_total": output_total,
        "aggregate_prompt_tokens_per_s": (
            prompt_total / wall_s if wall_s > 0 else None
        ),
        "aggregate_output_tokens_per_s": (
            aggregate_tps if math.isfinite(aggregate_tps) else None
        ),
        "aggregate_output_tokens_per_s_per_user": (
            aggregate_tps / batch_size
            if batch_size > 0 and math.isfinite(aggregate_tps)
            else None
        ),
        "request_output_tokens_per_s_after_first_mean": mean_or_none(
            item.output_tokens_per_s_after_first for item in good
        ),
        "request_output_tokens_per_s_after_first_p50": percentile(
            (item.output_tokens_per_s_after_first for item in good), 0.50
        ),
        "request_output_tokens_per_s_e2e_mean": mean_or_none(
            item.output_tokens_per_s_e2e for item in good
        ),
        "ttft_mean_s": mean_or_none(item.ttft_s for item in good),
        "ttft_p50_s": percentile((item.ttft_s for item in good), 0.50),
        "ttft_p95_s": percentile((item.ttft_s for item in good), 0.95),
        "e2e_p50_s": percentile((item.e2e_s for item in good), 0.50),
        "e2e_p95_s": percentile((item.e2e_s for item in good), 0.95),
        "prompt_tokens_min": (
            min((item.reported_prompt_tokens for item in good), default=None)
        ),
        "prompt_tokens_max": (
            max((item.reported_prompt_tokens for item in good), default=None)
        ),
        "completion_tokens_min": (
            min((item.completion_tokens for item in good), default=None)
        ),
        "completion_tokens_max": (
            max((item.completion_tokens for item in good), default=None)
        ),
        "finish_reason_counts": dict(finish_counts),
    }


def summarize_concurrency(
    label: str,
    batch_size: int,
    wave_rows: list[dict[str, Any]],
    results: list[RequestResult],
) -> dict[str, Any]:
    selected_waves = [row for row in wave_rows if row["batch_size"] == batch_size]
    selected = [
        item
        for item in results
        if item.stage == "measure" and item.batch_size == batch_size
    ]
    good = [item for item in selected if item.ok]
    total_wall = sum(float(row["batch_wall_s"]) for row in selected_waves)
    total_output = sum(item.completion_tokens for item in good)
    total_prompt = sum(item.reported_prompt_tokens for item in good)
    aggregate_tps = total_output / total_wall if total_wall > 0 else math.nan
    finish_counts = Counter(item.finish_reason or "<missing>" for item in selected)
    return {
        "label": label,
        "batch_size": batch_size,
        "repeats": len(selected_waves),
        "requests": len(selected),
        "successful": len(good),
        "batch_wall_s_total": total_wall,
        "reported_prompt_tokens_total": total_prompt,
        "completion_tokens_total": total_output,
        "aggregate_prompt_tokens_per_s": (
            total_prompt / total_wall if total_wall > 0 else None
        ),
        "aggregate_output_tokens_per_s": (
            aggregate_tps if math.isfinite(aggregate_tps) else None
        ),
        "aggregate_output_tokens_per_s_per_user": (
            aggregate_tps / batch_size
            if batch_size > 0 and math.isfinite(aggregate_tps)
            else None
        ),
        "request_output_tokens_per_s_after_first_mean": mean_or_none(
            item.output_tokens_per_s_after_first for item in good
        ),
        "request_output_tokens_per_s_after_first_p50": percentile(
            (item.output_tokens_per_s_after_first for item in good), 0.50
        ),
        "request_output_tokens_per_s_after_first_p95": percentile(
            (item.output_tokens_per_s_after_first for item in good), 0.95
        ),
        "request_output_tokens_per_s_e2e_mean": mean_or_none(
            item.output_tokens_per_s_e2e for item in good
        ),
        "ttft_mean_s": mean_or_none(item.ttft_s for item in good),
        "ttft_p50_s": percentile((item.ttft_s for item in good), 0.50),
        "ttft_p95_s": percentile((item.ttft_s for item in good), 0.95),
        "e2e_p50_s": percentile((item.e2e_s for item in good), 0.50),
        "e2e_p95_s": percentile((item.e2e_s for item in good), 0.95),
        "prompt_tokens_min": (
            min((item.reported_prompt_tokens for item in good), default=None)
        ),
        "prompt_tokens_max": (
            max((item.reported_prompt_tokens for item in good), default=None)
        ),
        "completion_tokens_min": (
            min((item.completion_tokens for item in good), default=None)
        ),
        "completion_tokens_max": (
            max((item.completion_tokens for item in good), default=None)
        ),
        "finish_reason_counts": dict(finish_counts),
    }


def csv_safe_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe: list[dict[str, Any]] = []
    for row in rows:
        converted = dict(row)
        converted["finish_reason_counts"] = json.dumps(
            converted["finish_reason_counts"], sort_keys=True
        )
        safe.append(converted)
    return safe


def csv_safe_requests(results: list[RequestResult]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        row = asdict(result)
        for key, value in list(row.items()):
            if isinstance(value, float) and not math.isfinite(value):
                row[key] = ""
        rows.append(row)
    return rows


async def get_model(
    session: aiohttp.ClientSession,
    endpoint: str,
    headers: dict[str, str],
    requested_model: str | None,
) -> str:
    if requested_model:
        return requested_model
    async with session.get(f"{endpoint}/v1/models", headers=headers) as response:
        text = await response.text()
        if response.status != 200:
            raise RuntimeError(
                f"/v1/models returned HTTP {response.status}: {text[:1000]}"
            )
        payload = json.loads(text)
        try:
            return str(payload["data"][0]["id"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"invalid /v1/models response: {text[:1000]}") from exc


async def fetch_metrics(
    session: aiohttp.ClientSession,
    endpoint: str,
    headers: dict[str, str],
) -> tuple[str | None, str | None]:
    try:
        async with session.get(f"{endpoint}/metrics", headers=headers) as response:
            text = await response.text()
            if response.status != 200:
                return None, f"HTTP {response.status}: {text[:1000]}"
            return text, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def metric_totals(prometheus_text: str | None) -> dict[str, float]:
    if not prometheus_text:
        return {}
    result: dict[str, float] = {}
    for name in _SPEC_METRICS:
        matches = re.findall(
            rf"^{re.escape(name)}(?:\{{[^}}]*\}})?\s+([0-9.eE+-]+)$",
            prometheus_text,
            flags=re.MULTILINE,
        )
        result[name] = sum(float(value) for value in matches)
    return result


def metric_delta(
    before_text: str | None, after_text: str | None
) -> dict[str, float]:
    before = metric_totals(before_text)
    after = metric_totals(after_text)
    return {
        name: after.get(name, 0.0) - before.get(name, 0.0)
        for name in _SPEC_METRICS
        if name in before or name in after
    }


def configuration(args: argparse.Namespace, endpoint: str, model: str) -> dict[str, Any]:
    return {
        "created_at": utc_now(),
        "label": args.label,
        "endpoint": endpoint,
        "model": model,
        "concurrency": list(args.concurrency),
        "repeats": args.repeats,
        "target_prompt_tokens": args.prompt_tokens,
        "prompt_tolerance": args.prompt_tolerance,
        "max_output_tokens": args.output_tokens,
        "min_output_tokens": (
            args.output_tokens if not args.honor_eos else 0
        ),
        "ignore_eos": not args.honor_eos,
        "reasoning_effort": "max",
        "temperature": 1.0,
        "top_p": 1.0,
        "thinking": True,
        "warmup_output_tokens": (
            0 if args.no_warmup else args.warmup_output_tokens
        ),
        "timeout_seconds": args.timeout,
        "seed": args.seed,
        "tool_execution": "disabled; calls are recorded only",
    }


def write_run_outputs(
    output_dir: Path,
    config: dict[str, Any],
    prompts: list[PromptCase],
    wave_rows: list[dict[str, Any]],
    results: list[RequestResult],
    metrics: dict[str, Any],
    status: str,
) -> None:
    by_concurrency = [
        summarize_concurrency(
            config["label"], batch_size, wave_rows, results
        )
        for batch_size in config["concurrency"]
        if any(row["batch_size"] == batch_size for row in wave_rows)
    ]
    summary = {
        "status": status,
        "updated_at": utc_now(),
        "configuration": config,
        "prompt_calibration": [
            {
                "case_index": prompt.case_index,
                "token_count": prompt.token_count,
                "pad_words": prompt.pad_words,
            }
            for prompt in prompts
        ],
        "waves": wave_rows,
        "by_concurrency": by_concurrency,
        "metrics": metrics,
        "artifacts": {
            "request_bodies": "requests/*.request.json",
            "byte_exact_sse": "responses/*.response.sse",
            "timed_parsed_sse_events": "responses/*.events.jsonl",
            "reconstructed_responses": "responses/*.response.json",
            "per_request_table": "requests.csv",
            "per_wave_table": "waves.csv",
            "concurrency_summary_table": "summary.csv",
        },
    }
    json_write(output_dir / "summary.json", summary)
    csv_write(output_dir / "summary.csv", csv_safe_summary(by_concurrency))
    csv_write(output_dir / "waves.csv", csv_safe_summary(wave_rows))
    csv_write(output_dir / "requests.csv", csv_safe_requests(results))


async def run(args: argparse.Namespace) -> int:
    if aiohttp is None:
        raise RuntimeError(
            "aiohttp is required for live benchmarks; install requirements.txt"
        )
    endpoint = normalize_endpoint(args.endpoint)
    output_dir = args.output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise RuntimeError(
            f"output directory is not empty: {output_dir}; choose a new path"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "requests").mkdir()
    (output_dir / "responses").mkdir()

    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
    timeout = aiohttp.ClientTimeout(
        total=args.timeout,
        connect=min(60.0, args.timeout),
        sock_connect=min(60.0, args.timeout),
        sock_read=args.timeout,
    )
    connector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    headers = safe_headers(api_key)
    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector, read_bufsize=2**20
    ) as session:
        model = await get_model(
            session, endpoint, headers, requested_model=args.model
        )
        config = configuration(args, endpoint, model)
        json_write(
            output_dir / "run_manifest.json",
            {
                "status": "calibrating",
                "updated_at": utc_now(),
                "configuration": config,
            },
        )

        tokenizer = TokenizerClient(session, endpoint, model, headers)
        await tokenizer.discover()
        prompt_count = max(args.concurrency)
        prompts = list(
            await asyncio.gather(
                *[
                    calibrate_prompt(
                        tokenizer=tokenizer,
                        case_index=index,
                        target=args.prompt_tokens,
                        tolerance=args.prompt_tolerance,
                        maximum_pad_words=args.maximum_pad_words,
                    )
                    for index in range(prompt_count)
                ]
            )
        )
        json_write(
            output_dir / "calibrated_prompts.json",
            {
                "tokenize_path": tokenizer.path,
                "target": args.prompt_tokens,
                "tolerance": args.prompt_tolerance,
                "tools": TOOLS,
                "prompts": [asdict(prompt) for prompt in prompts],
            },
        )
        print(
            "calibrated prompts: "
            f"min={min(p.token_count for p in prompts)}, "
            f"max={max(p.token_count for p in prompts)}, "
            f"tokenizer={tokenizer.path}",
            flush=True,
        )

        before_metrics, before_error = await fetch_metrics(
            session, endpoint, headers
        )
        if before_metrics is not None:
            (output_dir / "metrics_before.prom").write_text(
                before_metrics, encoding="utf-8"
            )

        results: list[RequestResult] = []
        wave_rows: list[dict[str, Any]] = []
        metrics_record: dict[str, Any] = {
            "before_error": before_error,
            "after_error": None,
            "delta": {},
        }
        write_run_outputs(
            output_dir, config, prompts, wave_rows, results, metrics_record, "running"
        )

        for batch_size in args.concurrency:
            if not args.no_warmup:
                warm_results, warm_wall = await run_wave(
                    session=session,
                    endpoint=endpoint,
                    api_key=api_key,
                    output_dir=output_dir,
                    prompts=prompts,
                    model=model,
                    stage="warmup",
                    batch_size=batch_size,
                    repeat=0,
                    output_tokens=args.warmup_output_tokens,
                    seed=args.seed + 1_000_000 + batch_size * 100,
                    force_exact_length=True,
                    prompt_tolerance=args.prompt_tolerance,
                )
                results.extend(warm_results)
                warm_ok = sum(item.ok for item in warm_results)
                print(
                    f"warmup c={batch_size}: {warm_ok}/{batch_size} ok "
                    f"in {warm_wall:.3f}s",
                    flush=True,
                )
                if warm_ok != batch_size and not args.continue_on_error:
                    write_run_outputs(
                        output_dir,
                        config,
                        prompts,
                        wave_rows,
                        results,
                        metrics_record,
                        "warmup_failed",
                    )
                    return 2

            for repeat in range(1, args.repeats + 1):
                measured, wall_s = await run_wave(
                    session=session,
                    endpoint=endpoint,
                    api_key=api_key,
                    output_dir=output_dir,
                    prompts=prompts,
                    model=model,
                    stage="measure",
                    batch_size=batch_size,
                    repeat=repeat,
                    output_tokens=args.output_tokens,
                    seed=args.seed + repeat * 10_000 + batch_size * 100,
                    force_exact_length=not args.honor_eos,
                    prompt_tolerance=args.prompt_tolerance,
                )
                results.extend(measured)
                row = summarize_wave(
                    args.label, batch_size, repeat, wall_s, measured
                )
                wave_rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                write_run_outputs(
                    output_dir,
                    config,
                    prompts,
                    wave_rows,
                    results,
                    metrics_record,
                    "running",
                )

        after_metrics, after_error = await fetch_metrics(
            session, endpoint, headers
        )
        if after_metrics is not None:
            (output_dir / "metrics_after.prom").write_text(
                after_metrics, encoding="utf-8"
            )
        metrics_record = {
            "before_error": before_error,
            "after_error": after_error,
            "delta": metric_delta(before_metrics, after_metrics),
        }
        draft_steps = metrics_record["delta"].get(
            "vllm:spec_decode_num_drafts_total", 0.0
        )
        draft_tokens = metrics_record["delta"].get(
            "vllm:spec_decode_num_draft_tokens_total", 0.0
        )
        accepted = metrics_record["delta"].get(
            "vllm:spec_decode_num_accepted_tokens_total", 0.0
        )
        metrics_record["accepted_tokens_per_draft_step"] = (
            accepted / draft_steps if draft_steps else None
        )
        metrics_record["draft_token_acceptance_percent"] = (
            100.0 * accepted / draft_tokens if draft_tokens else None
        )

        measured_results = [item for item in results if item.stage == "measure"]
        failed = [item for item in measured_results if not item.ok]
        final_status = "completed" if not failed else "completed_with_failures"
        write_run_outputs(
            output_dir,
            config,
            prompts,
            wave_rows,
            results,
            metrics_record,
            final_status,
        )
        json_write(
            output_dir / "run_manifest.json",
            {
                "status": final_status,
                "updated_at": utc_now(),
                "configuration": config,
                "measured_requests": len(measured_results),
                "failed_measured_requests": len(failed),
            },
        )
        print(
            json.dumps(
                {
                    "status": final_status,
                    "output_dir": str(output_dir),
                    "measured_requests": len(measured_results),
                    "failed_measured_requests": len(failed),
                },
                indent=2,
            ),
            flush=True,
        )
        return 0 if not failed else 2


def dry_run(args: argparse.Namespace) -> int:
    prompt_count = max(args.concurrency)
    messages = build_messages(0, 0)
    payload = build_chat_payload(
        model=args.model or "dry-run-model",
        messages=messages,
        output_tokens=args.output_tokens,
        seed=args.seed,
        force_exact_length=not args.honor_eos,
    )
    assert tuple(args.concurrency) == tuple(sorted(set(args.concurrency)))
    assert payload["reasoning_effort"] == "max"
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 1.0
    assert payload["max_tokens"] == args.output_tokens
    assert payload["stream_options"]["include_usage"] is True
    assert payload["tool_choice"] == "auto"
    if not args.honor_eos:
        assert payload["min_tokens"] == args.output_tokens
        assert payload["ignore_eos"] is True
    print(
        json.dumps(
            {
                "dry_run": True,
                "network_calls": 0,
                "files_written": 0,
                "concurrency": list(args.concurrency),
                "repeats": args.repeats,
                "prompt_variants_to_calibrate": prompt_count,
                "target_prompt_tokens": args.prompt_tokens,
                "output_tokens": args.output_tokens,
                "warmup_output_tokens": (
                    0 if args.no_warmup else args.warmup_output_tokens
                ),
                "reasoning_effort": payload["reasoning_effort"],
                "temperature": payload["temperature"],
                "top_p": payload["top_p"],
                "force_exact_length": not args.honor_eos,
                "tool_execution": "disabled",
                "base_message_characters": sum(
                    len(message["content"]) for message in messages
                ),
                "note": (
                    "Actual token counts are obtained from the server's "
                    "chat-aware /tokenize endpoint during a real run."
                ),
            },
            indent=2,
        )
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark DeepSeek V4 through a vLLM OpenAI-compatible endpoint "
            "with byte-exact streaming artifacts."
        )
    )
    parser.add_argument("--endpoint", default="http://127.0.0.1:8889")
    parser.add_argument("--model")
    parser.add_argument("--label", default="deepseek-v4-nvfp4")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--concurrency",
        type=int,
        nargs="+",
        default=list(DEFAULT_CONCURRENCY),
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--prompt-tokens", type=int, default=DEFAULT_PROMPT_TOKENS)
    parser.add_argument("--prompt-tolerance", type=int, default=12)
    parser.add_argument("--maximum-pad-words", type=int, default=2048)
    parser.add_argument("--output-tokens", type=int, default=DEFAULT_OUTPUT_TOKENS)
    parser.add_argument("--warmup-output-tokens", type=int, default=32)
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument(
        "--honor-eos",
        action="store_true",
        help=(
            "Allow early EOS. The default forces exactly --output-tokens via "
            "vLLM's min_tokens and ignore_eos extensions."
        ),
    )
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--timeout", type=float, default=7200.0)
    parser.add_argument("--seed", type=int, default=260729)
    parser.add_argument(
        "--api-key-env",
        default="VLLM_API_KEY",
        help=(
            "Name of an environment variable containing the local vLLM API "
            "key. Its value is never written to artifacts."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate request construction without network calls or file writes.",
    )
    args = parser.parse_args(argv)
    if not args.concurrency or any(value < 1 for value in args.concurrency):
        parser.error("--concurrency values must all be positive")
    if args.concurrency != sorted(set(args.concurrency)):
        parser.error("--concurrency must be unique and sorted")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.prompt_tokens < 1 or args.output_tokens < 1:
        parser.error("token counts must be positive")
    if args.prompt_tolerance < 0:
        parser.error("--prompt-tolerance cannot be negative")
    if args.warmup_output_tokens < 1:
        parser.error("--warmup-output-tokens must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.dry_run and args.output_dir is None:
        parser.error("--output-dir is required unless --dry-run is used")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.dry_run:
        return dry_run(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted; completed artifacts remain in the output directory", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
