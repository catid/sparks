#!/usr/bin/env python3
"""Check the Laguna replica router and optionally send a tagged inference."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid


METRIC_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?P<labels>\{[^}]*\})?"
    r"\s+(?P<value>[-+0-9.eE]+)(?:\s|$)"
)
ROUTING_COUNTERS = {
    "vllm_router_policy_decisions_total",
    "vllm_router_processed_requests_total",
    "vllm_router_requests_total",
}
ROUTER_GAUGES = {
    "vllm_router_active_workers",
    "vllm_router_cb_state",
    "vllm_router_worker_health",
    "vllm_router_worker_load",
}


def request(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, str]:
    data = None if body is None else json.dumps(body).encode()
    request_headers = {"Accept": "application/json"}
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")


def try_request(url: str, timeout: float = 5.0) -> tuple[int | None, str]:
    try:
        return request(url, timeout=timeout)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        return None, str(error)


def metrics(url: str) -> tuple[dict[str, float], str | None]:
    status, text = try_request(url)
    if status != 200:
        return {}, f"HTTP {status}: {text}"
    samples: dict[str, float] = {}
    for line in text.splitlines():
        match = METRIC_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name not in ROUTING_COUNTERS | ROUTER_GAUGES:
            continue
        key = name + (match.group("labels") or "")
        samples[key] = float(match.group("value"))
    return samples, None


def routing_deltas(
    before: dict[str, float], after: dict[str, float], heading: str
) -> set[str]:
    print(heading)
    changed = False
    selected_workers: set[str] = set()
    for key in sorted(set(before) | set(after)):
        if not any(key.startswith(name) for name in ROUTING_COUNTERS):
            continue
        delta = after.get(key, 0.0) - before.get(key, 0.0)
        if not delta:
            continue
        changed = True
        print(f"  {key} +{delta:g}")
        if key.startswith("vllm_router_processed_requests_total"):
            worker = re.search(r'worker="([^"]+)"', key)
            if worker:
                selected_workers.add(worker.group(1))
    if not changed:
        print("  no routing counter changed")
    return selected_workers


def print_status(
    router_url: str, metrics_url: str, workers: list[str]
) -> tuple[dict[str, float], bool]:
    ok = True
    print("Backends:")
    for worker in workers:
        status, detail = try_request(worker.rstrip("/") + "/health")
        state = "healthy" if status == 200 else "unavailable"
        print(f"  {worker}: {state} (HTTP {status})")
        if status != 200:
            ok = False
            if detail:
                print(f"    {detail[:180]}")

    print("Router:")
    for endpoint in ("/liveness", "/readiness", "/health"):
        status, detail = try_request(router_url.rstrip("/") + endpoint)
        print(f"  {endpoint}: HTTP {status}")
        if status != 200 and detail:
            print(f"    {detail[:180]}")
    current, error = metrics(metrics_url)
    if error:
        print(f"  metrics: unavailable ({error})")
        ok = False
    else:
        print(f"  metrics: {metrics_url}")
        gauges = {key: value for key, value in current.items() if any(
            key.startswith(name) for name in ROUTER_GAUGES
        )}
        for key, value in sorted(gauges.items()):
            print(f"    {key} {value:g}")
    return current, ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check both vLLM backends and the router. With --send, snapshot "
            "router counters before and after a tagged chat request so the "
            "selected worker is visible."
        )
    )
    parser.add_argument(
        "--router-url", default=os.getenv("ROUTER_URL", "http://127.0.0.1:8080")
    )
    parser.add_argument(
        "--metrics-url",
        default=os.getenv("ROUTER_METRICS_URL", "http://127.0.0.1:29000/metrics"),
    )
    parser.add_argument(
        "--workers",
        nargs="+",
        default=[
            os.getenv("ROUTER_WORKER_1", "http://192.168.100.10:8000"),
            os.getenv("ROUTER_WORKER_2", "http://192.168.100.11:8000"),
        ],
    )
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument(
        "--distinct-sessions",
        type=int,
        default=0,
        help=(
            "After the affinity check, send this many one-shot sessions and "
            "verify that the hash ring uses both replicas."
        ),
    )
    parser.add_argument(
        "--model",
        default=os.getenv("ROUTER_MODEL", "poolside/Laguna-S-2.1-NVFP4"),
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: router verification successful",
    )
    parser.add_argument("--max-tokens", type=int, default=32)
    parser.add_argument(
        "--session-id",
        default=None,
        help="Reuse this value to test affinity for a multi-turn agent session.",
    )
    args = parser.parse_args()

    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if args.distinct_sessions < 0:
        parser.error("--distinct-sessions cannot be negative")

    before, status_ok = print_status(
        args.router_url, args.metrics_url, args.workers
    )
    if not args.send:
        return 0 if status_ok else 1

    def send_inference(session_id: str, index: int) -> bool:
        request_id = f"{session_id}-{index + 1}"
        payload = {
            "model": args.model,
            "messages": [{"role": "user", "content": args.prompt}],
            "temperature": 0,
            "max_tokens": args.max_tokens,
            "stream": False,
        }
        started = time.monotonic()
        try:
            status, text = request(
                args.router_url.rstrip("/") + "/v1/chat/completions",
                method="POST",
                body=payload,
                headers={
                    "X-Request-ID": request_id,
                    "X-Session-ID": session_id,
                },
                timeout=3600,
            )
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            status, text = None, str(error)
        elapsed = time.monotonic() - started
        print(f"  {request_id}: HTTP {status}, {elapsed:.2f}s")
        if status != 200:
            print(f"    {text[:500]}")
            return False
        try:
            response = json.loads(text)
            content = response["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            content = text
        print(f"    {str(content)[:500]}")
        return True

    session_id = args.session_id or f"verify-{uuid.uuid4()}"
    print(f"Affinity inference (session {session_id}):")
    inference_ok = True
    for index in range(args.repeat):
        inference_ok = send_inference(session_id, index) and inference_ok

    after_affinity, metrics_error = metrics(args.metrics_url)
    if metrics_error:
        print(f"Affinity routing counters unavailable: {metrics_error}")
        affinity_workers: set[str] = set()
    else:
        affinity_workers = routing_deltas(
            before,
            after_affinity,
            "Affinity counter deltas (one worker proves session pinning):",
        )
    affinity_ok = len(affinity_workers) == 1

    distinct_workers: set[str] = set()
    if args.distinct_sessions:
        distinct_base = f"verify-distinct-{uuid.uuid4()}"
        print(f"Distribution inference ({args.distinct_sessions} distinct sessions):")
        for index in range(args.distinct_sessions):
            distinct_id = f"{distinct_base}-{index + 1}"
            inference_ok = send_inference(distinct_id, 0) and inference_ok
        after_distinct, metrics_error = metrics(args.metrics_url)
        if metrics_error:
            print(f"Distribution routing counters unavailable: {metrics_error}")
        else:
            distinct_workers = routing_deltas(
                after_affinity,
                after_distinct,
                "Distribution counter deltas (both workers prove multiplexing):",
            )

    distribution_ok = not args.distinct_sessions or len(distinct_workers) == len(
        args.workers
    )
    return (
        0
        if status_ok and inference_ok and affinity_ok and distribution_ok
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
