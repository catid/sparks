#!/usr/bin/env python3
"""Small, read-only cluster metrics server for the Cerebrus 3 kiosk.

The collector has no third-party dependencies.  It samples the local host
directly, samples the other configured hosts over the existing cluster SSH
trust, and derives live generation-token throughput from the cumulative vLLM
counter exported by the production endpoint.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import math
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import fmean
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
DEFAULT_NODES = ("cerebrus1", "cerebrus2", "cerebrus3")
DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_PORT = 9763
MAX_METRICS_BYTES = 4 * 1024 * 1024
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PROM_SAMPLE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?:\{[^}]*\})?\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]Inf)"
    r"(?:\s+\d+)?$"
)
PROM_TYPE = re.compile(
    r"^#\s+TYPE\s+(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)\s+"
    r"(?P<type>counter|gauge|histogram|summary|untyped)\s*$"
)

# The total is preferred.  The histogram sum is a compatible fallback for
# vLLM versions that omit generation_tokens_total.  Only one source is used,
# so equivalent counters are never added together and double-counted.
GENERATION_COUNTER_CANDIDATES = (
    ("vllm:generation_tokens_total", "vllm:generation_tokens_total", "counter"),
    ("vllm_generation_tokens_total", "vllm_generation_tokens_total", "counter"),
    (
        "vllm:request_generation_tokens_sum",
        "vllm:request_generation_tokens",
        "histogram",
    ),
    (
        "vllm_request_generation_tokens_sum",
        "vllm_request_generation_tokens",
        "histogram",
    ),
)

# This deliberately emits cumulative CPU counters.  Utilization is computed
# across successive five-second polls in the collector, rather than treating
# a lifetime counter as a current percentage.
REMOTE_PROBE = r"""
set -u
printf 'HOSTNAME=%s\n' "$(hostname -s 2>/dev/null || hostname)"
awk '/^cpu / {
  idle = $5 + $6
  total = 0
  for (field = 2; field <= 9; field++) total += $field
  printf "CPU=%.0f,%.0f\n", total, idle
  exit
}' /proc/stat
awk '/^MemTotal:/ { total = $2 }
     /^MemAvailable:/ { available = $2 }
     END {
       if (total != "" && available != "")
         printf "MEMORY=%.0f,%.0f\n", total, available
     }' /proc/meminfo
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=utilization.gpu \
    --format=csv,noheader,nounits 2>/dev/null |
    sed 's/^/GPU=/' || true
fi
"""


def utc_timestamp(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def percent(value: float | None) -> float | None:
    if value is None:
        return None
    return round(min(100.0, max(0.0, value)), 1)


def canonical_host(host: str) -> str:
    short = host.strip().lower().split(".", 1)[0]
    aliases = {"spark1": "cerebrus1", "spark2": "cerebrus2", "spark3": "cerebrus3"}
    return aliases.get(short, short)


def validate_nodes(nodes: tuple[str, ...]) -> tuple[str, ...]:
    if not nodes:
        raise ValueError("at least one dashboard node is required")
    if any(not HOST_PATTERN.fullmatch(node) for node in nodes):
        raise ValueError("node names may contain only letters, digits, dot, dash, and underscore")
    if len({canonical_host(node) for node in nodes}) != len(nodes):
        raise ValueError("dashboard node names must be unique")
    return nodes


def parse_probe(text: str) -> dict[str, Any]:
    """Parse the fixed probe's deliberately small line protocol."""
    result: dict[str, Any] = {
        "reported_hostname": None,
        "cpu_total": None,
        "cpu_idle": None,
        "gpu_percent": None,
        "ram_total_bytes": None,
        "ram_available_bytes": None,
    }
    gpu_values: list[float] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("HOSTNAME="):
            result["reported_hostname"] = line.split("=", 1)[1].strip() or None
        elif line.startswith("CPU="):
            fields = line.split("=", 1)[1].split(",")
            if len(fields) == 2:
                total = finite_float(fields[0])
                idle = finite_float(fields[1])
                if total is not None and idle is not None and total >= idle >= 0:
                    result["cpu_total"] = total
                    result["cpu_idle"] = idle
        elif line.startswith("MEMORY="):
            fields = line.split("=", 1)[1].split(",")
            if len(fields) == 2:
                total_kib = finite_float(fields[0])
                available_kib = finite_float(fields[1])
                if (
                    total_kib is not None
                    and available_kib is not None
                    and total_kib > 0
                    and 0 <= available_kib <= total_kib
                ):
                    result["ram_total_bytes"] = int(total_kib * 1024)
                    result["ram_available_bytes"] = int(available_kib * 1024)
        elif line.startswith("GPU="):
            value = finite_float(line.split("=", 1)[1].strip())
            if value is not None and 0 <= value <= 100:
                gpu_values.append(value)
    if gpu_values:
        result["gpu_percent"] = percent(fmean(gpu_values))
    return result


def cpu_percent(
    current_total: float | None,
    current_idle: float | None,
    previous: tuple[float, float] | None,
) -> float | None:
    """Derive CPU use from Linux cumulative jiffy counters, reset safely."""
    if current_total is None or current_idle is None or previous is None:
        return None
    previous_total, previous_idle = previous
    total_delta = current_total - previous_total
    idle_delta = current_idle - previous_idle
    if total_delta <= 0 or idle_delta < 0:
        return None
    return percent((total_delta - min(idle_delta, total_delta)) / total_delta * 100)


def parse_prometheus(text: str) -> tuple[dict[str, list[float]], dict[str, str]]:
    """Return finite samples and declared metric-family types."""
    samples: dict[str, list[float]] = defaultdict(list)
    types: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        type_match = PROM_TYPE.match(line)
        if type_match:
            types[type_match.group("name")] = type_match.group("type")
            continue
        if not line or line.startswith("#"):
            continue
        sample_match = PROM_SAMPLE.match(line)
        if not sample_match:
            continue
        value = finite_float(sample_match.group("value"))
        if value is not None:
            samples[sample_match.group("name")].append(value)
    return dict(samples), types


def generation_counter(text: str) -> tuple[float | None, str | None]:
    """Select one known cumulative generation-token source.

    A declared type that disagrees with the expected counter family is
    rejected.  This prevents a similarly named gauge from being differentiated
    as though it were a cumulative counter.
    """
    samples, types = parse_prometheus(text)
    for sample_name, family_name, expected_type in GENERATION_COUNTER_CANDIDATES:
        values = samples.get(sample_name)
        if not values:
            continue
        declared_type = types.get(family_name)
        if declared_type is not None and declared_type != expected_type:
            continue
        return sum(values), sample_name
    return None, None


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/plain", "User-Agent": "c3-cluster-dashboard/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = getattr(response, "status", 200)
        if status != 200:
            raise OSError(f"metrics endpoint returned HTTP {status}")
        body = response.read(MAX_METRICS_BYTES + 1)
    if len(body) > MAX_METRICS_BYTES:
        raise OSError("metrics response exceeded size limit")
    return body.decode("utf-8", "replace")


class ThroughputTracker:
    """Turn a vLLM lifetime token counter into an honest live rate."""

    def __init__(self, source: str, stale_after_seconds: float) -> None:
        self.source = source
        self.stale_after_seconds = max(stale_after_seconds, 1.0)
        self.previous_total: float | None = None
        self.previous_at: float | None = None
        self.last_success_at: float | None = None
        self.metric: str | None = None

    def _base(self, now: float) -> dict[str, Any]:
        return {
            "state": "warming",
            "healthy": False,
            "tokens_per_second": None,
            "token_kind": "generation",
            "metric_kind": "counter_rate",
            "source": self.source,
            "source_metric": self.metric,
            "age_seconds": (
                round(max(0.0, now - self.last_success_at), 1)
                if self.last_success_at is not None
                else None
            ),
            "window_seconds": None,
            "last_success_at": (
                utc_timestamp(self.last_success_at)
                if self.last_success_at is not None
                else None
            ),
            "stale_after_seconds": self.stale_after_seconds,
            "reason": "waiting for two cumulative-counter samples",
            "error": None,
        }

    def success(self, total: float, metric: str, now: float) -> dict[str, Any]:
        current = finite_float(total)
        if current is None or current < 0:
            return self.failure("invalid generation-token counter", now)

        old_total = self.previous_total
        old_at = self.previous_at
        self.previous_total = current
        self.previous_at = now
        self.last_success_at = now
        self.metric = metric
        result = self._base(now)
        result["healthy"] = True
        result["age_seconds"] = 0.0
        result["last_success_at"] = utc_timestamp(now)
        result["source_metric"] = metric

        if old_total is None or old_at is None:
            return result
        elapsed = now - old_at
        if current < old_total:
            result["reason"] = "counter reset; waiting for the next sample"
            return result
        if elapsed <= 0:
            result["reason"] = "non-positive sample interval; waiting for the next sample"
            return result

        rate = (current - old_total) / elapsed
        result.update(
            {
                "state": "active" if rate > 0 else "idle",
                "tokens_per_second": round(rate, 2),
                "window_seconds": round(elapsed, 3),
                "reason": "generation tokens are increasing" if rate > 0 else "no generation tokens in this interval",
            }
        )
        return result

    def failure(self, error: str, now: float) -> dict[str, Any]:
        # A rate spanning an unobserved scrape gap is not a current five-second
        # rate.  Discard the baseline so recovery warms up for one sample and
        # the next displayed value covers a fully observed window.
        self.previous_total = None
        self.previous_at = None
        result = self._base(now)
        age = (
            max(0.0, now - self.last_success_at)
            if self.last_success_at is not None
            else None
        )
        stale = age is not None and age < self.stale_after_seconds
        result.update(
            {
                "state": "stale" if stale else "down",
                "healthy": False,
                # Never carry the prior live rate through a failed scrape.
                "tokens_per_second": None,
                "age_seconds": round(age, 1) if age is not None else None,
                "reason": "last good sample is stale" if stale else "throughput telemetry is down",
                "error": error,
            }
        )
        return result


class HostProber:
    def __init__(
        self,
        ssh_key: str | None,
        known_hosts: str | None,
        local_hostname: str | None = None,
        timeout: float = 4.0,
    ) -> None:
        self.ssh_key = ssh_key
        self.known_hosts = known_hosts
        self.local_hostname = canonical_host(local_hostname or socket.gethostname())
        self.timeout = timeout

    def command(self, host: str) -> list[str]:
        if canonical_host(host) == self.local_hostname:
            return ["sh", "-s"]
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=2",
            "-o",
            "ConnectionAttempts=1",
        ]
        if self.ssh_key:
            command.extend(["-i", self.ssh_key])
        if self.known_hosts:
            command.extend(["-o", f"UserKnownHostsFile={self.known_hosts}"])
        command.extend([host, "sh -s"])
        return command

    def __call__(self, host: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                self.command(host),
                input=REMOTE_PROBE,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": str(exc)}
        if result.returncode:
            error = result.stderr.strip().splitlines()
            return {
                "error": error[-1] if error else f"probe exited {result.returncode}"
            }
        parsed = parse_probe(result.stdout)
        if parsed["cpu_total"] is None and parsed["ram_total_bytes"] is None:
            return {"error": "probe returned no CPU or RAM metrics"}
        return parsed


def average_field(hosts: dict[str, dict[str, Any]], field: str) -> tuple[float | None, int]:
    values = [
        value
        for host in hosts.values()
        if (value := finite_float(host.get(field))) is not None
    ]
    return (round(fmean(values), 1), len(values)) if values else (None, 0)


class Collector:
    def __init__(
        self,
        nodes: tuple[str, ...] = DEFAULT_NODES,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        metrics_url: str = "http://cerebrus1:8889/metrics",
        history_points: int = 720,
        host_prober: Callable[[str], dict[str, Any]] | None = None,
        metrics_fetcher: Callable[[str, float], str] = fetch_text,
        ssh_key: str | None = None,
        known_hosts: str | None = None,
    ) -> None:
        self.nodes = validate_nodes(nodes)
        self.interval = max(1.0, interval)
        self.metrics_url = metrics_url
        self.history_points = max(2, history_points)
        self.host_prober = host_prober or HostProber(ssh_key, known_hosts)
        self.metrics_fetcher = metrics_fetcher
        self.throughput_tracker = ThroughputTracker(
            # Keep endpoint details (including any future credentials) out of
            # the public status payload.  The exact Prometheus metric remains
            # visible through source_metric for diagnostics.
            "vllm", stale_after_seconds=max(2.5 * self.interval, 10.0)
        )
        self.previous_cpu: dict[str, tuple[float, float]] = {}
        self.last_host_success: dict[str, float] = {}
        self.history: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot = self._initial_snapshot()

    def _initial_snapshot(self) -> dict[str, Any]:
        hosts = {
            name: {
                "name": name,
                "reported_hostname": None,
                "state": "starting",
                "cpu_percent": None,
                "gpu_percent": None,
                "ram_percent": None,
                "ram_used_bytes": None,
                "ram_total_bytes": None,
                "sampled_at": None,
                "age_seconds": None,
                "error": None,
            }
            for name in self.nodes
        }
        return {
            "generated_at": None,
            "interval_seconds": self.interval,
            "collector": {"state": "starting", "error": None},
            "hosts": hosts,
            "cluster": {
                "state": "starting",
                "healthy": False,
                "available_hosts": 0,
                "total_hosts": len(self.nodes),
                "cpu_percent": None,
                "gpu_percent": None,
                "ram_percent": None,
                "sampled_hosts": {"cpu": 0, "gpu": 0, "ram": 0},
            },
            "throughput": self.throughput_tracker._base(time.time()),
            "history": [],
        }

    def _scrape_counter(self) -> dict[str, Any]:
        try:
            text = self.metrics_fetcher(self.metrics_url, min(3.0, self.interval))
            total, metric = generation_counter(text)
            if total is None or metric is None:
                return {"error": "no supported cumulative generation-token counter"}
            return {"total": total, "metric": metric}
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            return {"error": str(exc)}
        except Exception as exc:  # isolate a malformed response from host telemetry
            return {"error": f"metrics parse failed: {exc}"}

    def _host_status(self, name: str, raw: dict[str, Any], now: float) -> dict[str, Any]:
        if raw.get("error"):
            self.previous_cpu.pop(name, None)
            last_seen = self.last_host_success.get(name)
            return {
                "name": name,
                "reported_hostname": None,
                "state": "down",
                "cpu_percent": None,
                "gpu_percent": None,
                "ram_percent": None,
                "ram_used_bytes": None,
                "ram_total_bytes": None,
                "sampled_at": None,
                "age_seconds": (
                    round(max(0.0, now - last_seen), 1)
                    if last_seen is not None
                    else None
                ),
                "error": str(raw["error"]),
            }

        total = finite_float(raw.get("cpu_total"))
        idle = finite_float(raw.get("cpu_idle"))
        cpu = cpu_percent(total, idle, self.previous_cpu.get(name))
        if total is not None and idle is not None:
            self.previous_cpu[name] = (total, idle)

        ram_total = raw.get("ram_total_bytes")
        ram_available = raw.get("ram_available_bytes")
        ram_used: int | None = None
        ram = None
        if (
            isinstance(ram_total, int)
            and isinstance(ram_available, int)
            and ram_total > 0
            and 0 <= ram_available <= ram_total
        ):
            ram_used = ram_total - ram_available
            ram = percent(ram_used / ram_total * 100)

        self.last_host_success[name] = now
        return {
            "name": name,
            "reported_hostname": raw.get("reported_hostname"),
            "state": "up",
            "cpu_percent": cpu,
            "gpu_percent": percent(finite_float(raw.get("gpu_percent"))),
            "ram_percent": ram,
            "ram_used_bytes": ram_used,
            "ram_total_bytes": ram_total if isinstance(ram_total, int) else None,
            "sampled_at": utc_timestamp(now),
            "age_seconds": 0.0,
            "error": None,
        }

    def collect(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        raw_hosts: dict[str, dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.nodes) + 1,
            thread_name_prefix="c3-metric",
        ) as executor:
            host_futures = {
                name: executor.submit(self.host_prober, name) for name in self.nodes
            }
            counter_future = executor.submit(self._scrape_counter)
            for name, future in host_futures.items():
                try:
                    raw_hosts[name] = future.result()
                except Exception as exc:
                    raw_hosts[name] = {"error": f"probe failed: {exc}"}
            try:
                counter_sample = counter_future.result()
            except Exception as exc:
                counter_sample = {"error": f"metrics scrape failed: {exc}"}

        hosts = {
            name: self._host_status(name, raw_hosts[name], now) for name in self.nodes
        }
        cpu, cpu_count = average_field(hosts, "cpu_percent")
        gpu, gpu_count = average_field(hosts, "gpu_percent")
        ram, ram_count = average_field(hosts, "ram_percent")
        available = sum(host["state"] == "up" for host in hosts.values())
        cluster_state = (
            "up"
            if available == len(self.nodes)
            else "degraded"
            if available
            else "down"
        )
        cluster = {
            "state": cluster_state,
            "healthy": available == len(self.nodes),
            "available_hosts": available,
            "total_hosts": len(self.nodes),
            "cpu_percent": cpu,
            "gpu_percent": gpu,
            "ram_percent": ram,
            "sampled_hosts": {
                "cpu": cpu_count,
                "gpu": gpu_count,
                "ram": ram_count,
            },
        }

        if counter_sample.get("error"):
            throughput = self.throughput_tracker.failure(
                str(counter_sample["error"]), now
            )
        else:
            throughput = self.throughput_tracker.success(
                counter_sample["total"], counter_sample["metric"], now
            )

        generated_at = utc_timestamp(now)
        history_point = {
            "timestamp": generated_at,
            "cluster": {
                "cpu_percent": cluster["cpu_percent"],
                "gpu_percent": cluster["gpu_percent"],
                "ram_percent": cluster["ram_percent"],
            },
            "throughput": {
                "tokens_per_second": throughput["tokens_per_second"],
                "state": throughput["state"],
            },
            "hosts": {
                name: {
                    "cpu_percent": host["cpu_percent"],
                    "gpu_percent": host["gpu_percent"],
                    "ram_percent": host["ram_percent"],
                    "state": host["state"],
                }
                for name, host in hosts.items()
            },
        }
        with self.lock:
            self.history.append(history_point)
            if len(self.history) > self.history_points:
                del self.history[: len(self.history) - self.history_points]
            self.snapshot = {
                "generated_at": generated_at,
                "interval_seconds": self.interval,
                "collector": {"state": "ok", "error": None},
                "hosts": hosts,
                "cluster": cluster,
                "throughput": throughput,
                "history": copy.deepcopy(self.history),
            }

    def run(self) -> None:
        deadline = time.monotonic() + self.interval
        while True:
            delay = max(0.0, deadline - time.monotonic())
            if self.stop_event.wait(delay):
                return
            try:
                self.collect()
            except Exception as exc:  # a collector bug must not kill the kiosk HTTP server
                with self.lock:
                    self.snapshot["collector"] = {
                        "state": "error",
                        "error": str(exc),
                    }
            deadline += self.interval
            if deadline <= time.monotonic():
                deadline = time.monotonic() + self.interval

    def get_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.snapshot)


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "C3ClusterDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/api/status", "/api/status/"):
            body = json.dumps(
                getattr(self.server, "collector").get_snapshot(),
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
        )
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            f"{self.client_address[0]} - [{self.log_date_time_string()}] {fmt % args}",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host", default=os.environ.get("C3_DASHBOARD_HOST", "127.0.0.1")
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("C3_DASHBOARD_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(
            os.environ.get("C3_DASHBOARD_INTERVAL", str(DEFAULT_INTERVAL_SECONDS))
        ),
    )
    return parser.parse_args()


def optional_path(environment_name: str, default: Path) -> str | None:
    value = os.environ.get(environment_name)
    if value is None:
        return str(default)
    return value.strip() or None


def main() -> None:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("C3 dashboard port must be between 1 and 65535")
    if args.host not in ("127.0.0.1", "::1", "localhost") and os.environ.get(
        "C3_DASHBOARD_ALLOW_REMOTE"
    ) != "1":
        raise SystemExit(
            "Refusing a non-loopback bind; set C3_DASHBOARD_ALLOW_REMOTE=1 "
            "only on a trusted management network."
        )

    nodes = tuple(
        node.strip()
        for node in os.environ.get(
            "C3_DASHBOARD_NODES", ",".join(DEFAULT_NODES)
        ).split(",")
        if node.strip()
    )
    try:
        validate_nodes(nodes)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    user_ssh = Path.home() / ".ssh"
    collector = Collector(
        nodes=nodes,
        interval=args.interval,
        metrics_url=os.environ.get(
            "C3_DASHBOARD_VLLM_METRICS_URL",
            "http://cerebrus1:8889/metrics",
        ),
        history_points=int(os.environ.get("C3_DASHBOARD_HISTORY_POINTS", "720")),
        ssh_key=optional_path(
            "C3_DASHBOARD_SSH_KEY", user_ssh / "id_ed25519_dgx_cluster"
        ),
        known_hosts=optional_path(
            "C3_DASHBOARD_SSH_KNOWN_HOSTS",
            user_ssh / "dgx_cluster_known_hosts",
        ),
    )
    collector.collect()
    collector_thread = threading.Thread(
        target=collector.run, name="c3-collector", daemon=True
    )
    collector_thread.start()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.collector = collector  # type: ignore[attr-defined]
    print(
        f"C3 cluster dashboard listening on http://{args.host}:{args.port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
