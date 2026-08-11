#!/usr/bin/env python3
"""Project a private DeepSeek benchmark run into publish-safe metrics.

The benchmark's raw directory intentionally contains prompts, model reasoning,
response headers, request IDs, and byte-exact output.  This script uses an
allowlist: it copies only numeric workload and timing fields required to compare
runs.  It never copies the endpoint, discovered model/path, source label,
errors, prompts, headers, request IDs, or generated text.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any


PUBLIC_SCHEMA_VERSION = 1
PUBLIC_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

WORKLOAD_FIELDS = (
    "concurrency",
    "repeats",
    "target_prompt_tokens",
    "prompt_tolerance",
    "max_output_tokens",
    "min_output_tokens",
    "ignore_eos",
    "reasoning_effort",
    "temperature",
    "top_p",
    "thinking",
    "warmup_output_tokens",
    "seed",
    "tool_execution",
)

RESULT_FIELDS = (
    "batch_size",
    "repeats",
    "requests",
    "successful",
    "batch_wall_s_total",
    "reported_prompt_tokens_total",
    "completion_tokens_total",
    "aggregate_prompt_tokens_per_s",
    "aggregate_output_tokens_per_s",
    "aggregate_output_tokens_per_s_per_user",
    "request_output_tokens_per_s_after_first_mean",
    "request_output_tokens_per_s_after_first_p50",
    "request_output_tokens_per_s_after_first_p95",
    "request_output_tokens_per_s_e2e_mean",
    "ttft_mean_s",
    "ttft_p50_s",
    "ttft_p95_s",
    "e2e_p50_s",
    "e2e_p95_s",
    "prompt_tokens_min",
    "prompt_tokens_max",
    "completion_tokens_min",
    "completion_tokens_max",
)

WAVE_FIELDS = (
    "batch_size",
    "repeat",
    "requests",
    "successful",
    "batch_wall_s",
    "reported_prompt_tokens_total",
    "completion_tokens_total",
    "aggregate_prompt_tokens_per_s",
    "aggregate_output_tokens_per_s",
    "aggregate_output_tokens_per_s_per_user",
    "request_output_tokens_per_s_after_first_mean",
    "request_output_tokens_per_s_after_first_p50",
    "request_output_tokens_per_s_e2e_mean",
    "ttft_mean_s",
    "ttft_p50_s",
    "ttft_p95_s",
    "e2e_p50_s",
    "e2e_p95_s",
    "prompt_tokens_min",
    "prompt_tokens_max",
    "completion_tokens_min",
    "completion_tokens_max",
)

SPEC_COUNTERS = (
    "vllm:spec_decode_num_drafts_total",
    "vllm:spec_decode_num_draft_tokens_total",
    "vllm:spec_decode_num_accepted_tokens_total",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
)

KNOWN_FINISH_REASONS = ("length", "stop", "tool_calls", "<missing>")


def _safe_scalar(value: Any) -> int | float | bool | None:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    raise ValueError(f"expected a JSON scalar, got {type(value).__name__}")


def _project(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        field: _safe_scalar(source.get(field))
        for field in fields
        if field in source
    }


def _project_workload(source: dict[str, Any]) -> dict[str, Any]:
    result = _project(
        source,
        tuple(
            field
            for field in WORKLOAD_FIELDS
            if field not in {"concurrency", "reasoning_effort", "tool_execution"}
        ),
    )
    concurrency = source.get("concurrency")
    if not isinstance(concurrency, list) or not all(
        isinstance(item, int)
        and not isinstance(item, bool)
        and item > 0
        for item in concurrency
    ):
        raise ValueError("workload concurrency must be a list of positive integers")
    result["concurrency"] = concurrency
    reasoning_effort = source.get("reasoning_effort")
    result["reasoning_effort"] = (
        reasoning_effort
        if reasoning_effort in {"none", "minimal", "low", "medium", "high", "max"}
        else "unknown"
    )
    result["tool_execution"] = (
        "disabled"
        if source.get("tool_execution") == "disabled; calls are recorded only"
        else "unknown"
    )
    return result


def _finish_reason_counts(source: Any) -> dict[str, int]:
    if not isinstance(source, dict):
        return {}
    result = {name: 0 for name in KNOWN_FINISH_REASONS}
    other = 0
    for key, value in source.items():
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("finish-reason counts must be non-negative integers")
        if key in result:
            result[key] += value
        else:
            other += value
    result = {key: value for key, value in result.items() if value}
    if other:
        result["other"] = other
    return result


def _project_metric_delta(source: Any) -> dict[str, int | float | bool | None]:
    if not isinstance(source, dict):
        return {}
    return {
        name: _safe_scalar(source[name])  # type: ignore[dict-item]
        for name in SPEC_COUNTERS
        if name in source
    }


def _required_dict(source: dict[str, Any], key: str) -> dict[str, Any]:
    value = source.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"source summary is missing object {key!r}")
    return value


def _required_list(source: dict[str, Any], key: str) -> list[Any]:
    value = source.get(key)
    if not isinstance(value, list):
        raise ValueError(f"source summary is missing list {key!r}")
    return value


def build_public_summary(
    source: dict[str, Any],
    public_label: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Return an allowlisted summary that is safe to put in the public repo."""
    if not PUBLIC_LABEL.fullmatch(public_label):
        raise ValueError(
            "public label must be 1-96 letters, digits, dots, underscores, or "
            "hyphens, and start with a letter or digit"
        )
    if not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ValueError("source_sha256 must be a lowercase SHA-256 digest")

    config = _required_dict(source, "configuration")
    result_rows = _required_list(source, "by_concurrency")
    wave_rows = _required_list(source, "waves")
    calibration_rows = _required_list(source, "prompt_calibration")
    metrics = source.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}

    calibration_counts: list[int] = []
    for item in calibration_rows:
        if not isinstance(item, dict):
            raise ValueError("prompt calibration entries must be objects")
        count = item.get("token_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("prompt calibration token counts must be positive")
        calibration_counts.append(count)

    public_results = []
    for item in result_rows:
        if not isinstance(item, dict):
            raise ValueError("concurrency summary entries must be objects")
        projected = _project(item, RESULT_FIELDS)
        projected["finish_reason_counts"] = _finish_reason_counts(
            item.get("finish_reason_counts")
        )
        public_results.append(projected)

    public_waves = []
    for item in wave_rows:
        if not isinstance(item, dict):
            raise ValueError("wave summary entries must be objects")
        projected = _project(item, WAVE_FIELDS)
        projected["finish_reason_counts"] = _finish_reason_counts(
            item.get("finish_reason_counts")
        )
        public_waves.append(projected)

    speculative = {
        "counter_delta": _project_metric_delta(metrics.get("delta")),
        "accepted_tokens_per_draft_step": _safe_scalar(
            metrics.get("accepted_tokens_per_draft_step")
        ),
        "draft_token_acceptance_percent": _safe_scalar(
            metrics.get("draft_token_acceptance_percent")
        ),
    }

    status = source.get("status")
    if status not in {
        "running",
        "warmup_failed",
        "completed",
        "completed_with_failures",
    }:
        status = "unknown"

    return {
        "schema_version": PUBLIC_SCHEMA_VERSION,
        "benchmark": "deepseek-v4-fixed1024-coding-streaming",
        "label": public_label,
        "status": status,
        "workload": _project_workload(config),
        "prompt_calibration": {
            "cases": len(calibration_counts),
            "token_count_min": min(calibration_counts, default=None),
            "token_count_max": max(calibration_counts, default=None),
        },
        "by_concurrency": public_results,
        "waves": public_waves,
        "speculative_decoding": speculative,
        "provenance": {
            "source_summary_sha256": source_sha256,
            "projection": "allowlist-v1",
        },
    }


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_csv_write(path: Path, payload: dict[str, Any]) -> None:
    rows = payload["by_concurrency"]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fieldnames = ["label", "status", *RESULT_FIELDS, "finish_reason_counts"]
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for source_row in rows:
            row = {field: source_row.get(field) for field in RESULT_FIELDS}
            row["label"] = payload["label"]
            row["status"] = payload["status"]
            row["finish_reason_counts"] = json.dumps(
                source_row.get("finish_reason_counts", {}),
                sort_keys=True,
                separators=(",", ":"),
            )
            writer.writerow(row)
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create allowlisted public metrics from a private run."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--json-out", required=True, type=Path)
    parser.add_argument("--csv-out", required=True, type=Path)
    parser.add_argument("--label", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destinations = {
        args.source.resolve(),
        args.json_out.resolve(),
        args.csv_out.resolve(),
    }
    if len(destinations) != 3:
        raise ValueError("source, JSON output, and CSV output must be distinct paths")
    source_bytes = args.source.read_bytes()
    source = json.loads(source_bytes)
    if not isinstance(source, dict):
        raise ValueError("source summary must be a JSON object")
    projected = build_public_summary(
        source=source,
        public_label=args.label,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
    )
    _atomic_json_write(args.json_out, projected)
    _atomic_csv_write(args.csv_out, projected)
    print(
        json.dumps(
            {
                "public_json": str(args.json_out),
                "public_csv": str(args.csv_out),
                "rows": len(projected["by_concurrency"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
