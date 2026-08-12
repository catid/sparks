#!/usr/bin/env python3
"""Read-only two-node DGX Spark and vLLM dashboard.

The service intentionally uses only the Python standard library.  It samples
the local Spark directly, samples Cerberus 2 over the existing SSH trust, and
scrapes the configured vLLM/router Prometheus endpoints without mutating any
service.  Node roles make a tensor-parallel headless worker distinct from an
independent HTTP-serving replica.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import hashlib
import hmac
import http.client
import json
import math
import os
import re
import socket
import stat
import subprocess
import threading
import time
import urllib.parse
from collections import defaultdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_INTERFACES = (
    "enp1s0f0np0",
    "enP2p1s0f0np0",
    "enp1s0f1np1",
    "enP2p1s0f1np1",
)
PROM_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|NaN|[+-]Inf)"
    r"(?:\s+\d+)?$"
)
VALID_NODE_ROLES = frozenset({"aggregate", "replica", "worker"})
VALID_INFERENCE_MODES = frozenset({"direct", "router"})
CANONICAL_NODE_NAMES = ("cerberus1", "cerberus2")
LEGACY_NODE_ALIASES = {
    "spark1": "cerberus1",
    "spark2": "cerberus2",
}
GB10_CPU_THERMAL_ZONES = frozenset({"TS0E", "TS0P", "TS1E", "TS1P"})
MEMORY_HWMON_DRIVERS = frozenset({"jc42", "spd5118"})
RECOVERY_HEALTHY_SAMPLES = 2
MAX_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024


def canonical_node_name(name: str) -> str:
    """Map historical dashboard node keys to the canonical host identity."""
    return LEGACY_NODE_ALIASES.get(name, name)


def canonical_node_mapping(values: dict[str, Any]) -> dict[str, Any]:
    """Accept legacy node-map keys while preferring canonical values.

    Unknown keys are retained for callers that attach auxiliary metadata.
    When both forms are supplied, the explicit ``cerberusN`` value wins.
    """
    normalized = {
        name: value
        for name, value in values.items()
        if canonical_node_name(name) == name
    }
    for legacy, canonical in LEGACY_NODE_ALIASES.items():
        if canonical not in normalized and legacy in values:
            normalized[canonical] = values[legacy]
    return normalized


def utc_timestamp(timestamp: float) -> str:
    """Return a stable UTC timestamp for API status fields."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def readable_state(value: Any) -> str:
    """Turn internal state identifiers into short human-readable text."""
    return str(value or "unknown").strip().replace("_", " ")


def finite_float(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip().replace(" W", "").replace(" MHz", ""))
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def parse_prometheus(text: str) -> dict[str, list[float]]:
    """Parse numeric Prometheus exposition samples, grouped by metric name."""
    result: dict[str, list[float]] = defaultdict(list)
    for raw in text.splitlines():
        if not raw or raw.startswith("#"):
            continue
        match = PROM_SAMPLE.match(raw.strip())
        if not match:
            continue
        value = finite_float(match.group("value"))
        if value is not None:
            result[match.group("name")].append(value)
    return dict(result)


def metric_sum(metrics: dict[str, list[float]], *names: str) -> float | None:
    for name in names:
        values = metrics.get(name)
        if values:
            return sum(values)
    return None


def metric_avg(metrics: dict[str, list[float]], *names: str) -> float | None:
    for name in names:
        values = metrics.get(name)
        if values:
            return sum(values) / len(values)
    return None


def counter_rate(
    current: float | None, previous: float | None, elapsed: float
) -> float | None:
    if current is None or previous is None or elapsed <= 0 or current < previous:
        return None
    return (current - previous) / elapsed


def read_meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return values


def millidegree_c(value: str | float | int | None) -> float | None:
    """Convert a Linux thermal/hwmon millidegree value to sane Celsius."""
    parsed = finite_float(value)
    if parsed is None or not -20_000 <= parsed <= 150_000:
        return None
    return parsed / 1000


def summarize_thermals(
    samples: dict[str, list[float]],
) -> dict[str, Any]:
    """Summarize named GB10 firmware zones and optional hwmon devices."""

    def hottest(*names: str) -> float | None:
        values = [
            value
            for name in names
            for value in samples.get(name, ())
            if math.isfinite(value)
        ]
        return max(values) if values else None

    cpu_clusters = {
        name.lower(): hottest(name) for name in sorted(GB10_CPU_THERMAL_ZONES)
    }
    memory_c = hottest("MEMORY")
    values = [
        hottest("TSOC"),
        *cpu_clusters.values(),
        hottest("TGPU"),
        hottest("TUNC"),
        hottest("NVME"),
        hottest("MLX5"),
        memory_c,
    ]
    return {
        "available": any(value is not None for value in values),
        "source": "GB10 ACPI thermal zones and hwmon",
        "cpu_cluster_max_c": hottest(*sorted(GB10_CPU_THERMAL_ZONES)),
        "cpu_clusters_c": cpu_clusters,
        "soc_c": hottest("TSOC"),
        "firmware_gpu_c": hottest("TGPU"),
        "uncore_c": hottest("TUNC"),
        "nvme_composite_c": hottest("NVME"),
        "connectx_asic_max_c": hottest("MLX5"),
        "memory_c": memory_c,
        "memory_sensor_available": memory_c is not None,
    }


def thermal_stats(
    thermal_root: Path = Path("/sys/class/thermal"),
    hwmon_root: Path = Path("/sys/class/hwmon"),
) -> dict[str, Any]:
    """Read temperatures by firmware/hwmon identity, never by device order."""
    samples: dict[str, list[float]] = defaultdict(list)
    try:
        zones = tuple(thermal_root.glob("thermal_zone*"))
    except OSError:
        zones = ()
    for zone in zones:
        try:
            acpi_path = (zone / "device" / "path").read_text().strip()
            name = acpi_path.rsplit(".", 1)[-1].upper()
            value = millidegree_c((zone / "temp").read_text())
        except OSError:
            continue
        if value is not None:
            samples[name].append(value)

    try:
        devices = tuple(hwmon_root.glob("hwmon*"))
    except OSError:
        devices = ()
    for device in devices:
        try:
            driver = (device / "name").read_text().strip().lower()
        except OSError:
            continue
        for input_path in device.glob("temp*_input"):
            try:
                label = (
                    input_path.with_name(
                        input_path.name.removesuffix("_input") + "_label"
                    )
                    .read_text()
                    .strip()
                    .lower()
                )
            except OSError:
                label = ""
            try:
                value = millidegree_c(input_path.read_text())
            except OSError:
                continue
            if value is None:
                continue
            if driver == "nvme" and label == "composite":
                samples["NVME"].append(value)
            elif driver == "mlx5" and label == "asic":
                samples["MLX5"].append(value)
            elif driver in MEMORY_HWMON_DRIVERS:
                samples["MEMORY"].append(value)
    return summarize_thermals(samples)


def vllm_rss_bytes() -> int:
    total = 0
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ")
            if b"vllm" not in cmdline.lower():
                continue
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError, ValueError):
            continue
    return total


def gpu_stats() -> dict[str, Any]:
    fields = (
        "name,temperature.gpu,power.draw,clocks.sm,clocks.gr,"
        "utilization.gpu,utilization.memory,memory.used,memory.total"
    )
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={fields}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2.5,
            check=False,
        )
        parts = [part.strip() for part in result.stdout.splitlines()[0].split(",")]
        if len(parts) != 9:
            raise ValueError("unexpected nvidia-smi output")
        return {
            "available": True,
            "name": parts[0],
            "temperature_c": finite_float(parts[1]),
            "power_w": finite_float(parts[2]),
            "sm_clock_mhz": finite_float(parts[3]),
            "graphics_clock_mhz": finite_float(parts[4]),
            "gpu_util_percent": finite_float(parts[5]),
            "memory_util_percent": finite_float(parts[6]),
            "framebuffer_used_bytes": (
                finite_float(parts[7]) * 1024 * 1024
                if finite_float(parts[7]) is not None
                else None
            ),
            "framebuffer_total_bytes": (
                finite_float(parts[8]) * 1024 * 1024
                if finite_float(parts[8]) is not None
                else None
            ),
        }
    except (OSError, subprocess.SubprocessError, IndexError, ValueError) as exc:
        return {"available": False, "error": str(exc)}


def rdma_counters(interface: str) -> dict[str, Any] | None:
    """Read the RoCE port counters mapped to a Linux netdev.

    Ethernet netdev byte counters do not include RDMA data traffic. InfiniBand
    port data counters are expressed in 32-bit words, so convert them to bytes.
    """
    infiniband = Path("/sys/class/infiniband")
    try:
        devices = tuple(infiniband.iterdir())
    except OSError:
        return None
    for device in devices:
        if not (device / "device" / "net" / interface).exists():
            continue
        counters = device / "ports" / "1" / "counters"
        try:
            return {
                "rx_bytes": int((counters / "port_rcv_data").read_text()) * 4,
                "tx_bytes": int((counters / "port_xmit_data").read_text()) * 4,
                "rx_packets": int((counters / "port_rcv_packets").read_text()),
                "tx_packets": int((counters / "port_xmit_packets").read_text()),
                "counter_source": "rdma",
                "rdma_device": device.name,
            }
        except (OSError, ValueError):
            return None
    return None


def network_counters(interfaces: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for interface in interfaces:
        base = Path("/sys/class/net") / interface
        try:
            values = {
                "rx_bytes": int((base / "statistics/rx_bytes").read_text()),
                "tx_bytes": int((base / "statistics/tx_bytes").read_text()),
                "rx_errors": int((base / "statistics/rx_errors").read_text()),
                "tx_errors": int((base / "statistics/tx_errors").read_text()),
                "operstate": (base / "operstate").read_text().strip(),
                "mtu": int((base / "mtu").read_text()),
                "counter_source": "netdev",
            }
            values.update(rdma_counters(interface) or {})
            result[interface] = values
        except (OSError, ValueError):
            result[interface] = {"operstate": "unavailable"}
    return result


REMOTE_PROBE = r"""
set -eu
echo "HOSTNAME=$(hostname)"
nvidia-smi --query-gpu=name,temperature.gpu,power.draw,clocks.sm,clocks.gr,utilization.gpu,utilization.memory,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | sed 's/^/GPU=/'
awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {print "MEM_" $1 "=" $2 * 1024}' /proc/meminfo
for zone in /sys/class/thermal/thermal_zone*; do
  [ -r "$zone/device/path" ] && [ -r "$zone/temp" ] || continue
  thermal_path=$(cat "$zone/device/path" 2>/dev/null || true)
  thermal_value=$(cat "$zone/temp" 2>/dev/null || true)
  [ -n "$thermal_path" ] && [ -n "$thermal_value" ] &&
    printf 'THERMAL=%s,%s\n' "$thermal_path" "$thermal_value"
done
for hwmon in /sys/class/hwmon/hwmon*; do
  [ -r "$hwmon/name" ] || continue
  driver=$(cat "$hwmon/name" 2>/dev/null || true)
  for input in "$hwmon"/temp*_input; do
    [ -r "$input" ] || continue
    stem=${input%_input}
    label=$(cat "${stem}_label" 2>/dev/null || true)
    thermal_value=$(cat "$input" 2>/dev/null || true)
    [ -n "$thermal_value" ] || continue
    case "$driver:$label" in
      nvme:Composite) printf 'THERMAL=NVME,%s\n' "$thermal_value" ;;
      mlx5:asic) printf 'THERMAL=MLX5,%s\n' "$thermal_value" ;;
      jc42:*|spd5118:*) printf 'THERMAL=MEMORY,%s\n' "$thermal_value" ;;
    esac
  done
done
rss=$(ps -eo rss=,args= | awk 'tolower($0) ~ /vllm/ && tolower($0) !~ /awk/ {s += $1} END {printf "%.0f", s * 1024}')
echo "VLLM_RSS=${rss:-0}"
for nic in __INTERFACES__; do
  base="/sys/class/net/$nic"
  if [ -d "$base" ]; then
    rdma_dev=""
    for candidate in /sys/class/infiniband/*; do
      if [ -e "$candidate/device/net/$nic" ]; then
        rdma_dev=$(basename "$candidate")
        break
      fi
    done
    if [ -n "$rdma_dev" ]; then
      counters="/sys/class/infiniband/$rdma_dev/ports/1/counters"
      rx_bytes=$(( $(cat "$counters/port_rcv_data") * 4 ))
      tx_bytes=$(( $(cat "$counters/port_xmit_data") * 4 ))
      rx_packets=$(cat "$counters/port_rcv_packets")
      tx_packets=$(cat "$counters/port_xmit_packets")
      echo "NET=$nic,$rx_bytes,$tx_bytes,$(cat "$base/statistics/rx_errors"),$(cat "$base/statistics/tx_errors"),$(cat "$base/operstate"),$(cat "$base/mtu"),rdma,$rdma_dev,$rx_packets,$tx_packets"
    else
      echo "NET=$nic,$(cat "$base/statistics/rx_bytes"),$(cat "$base/statistics/tx_bytes"),$(cat "$base/statistics/rx_errors"),$(cat "$base/statistics/tx_errors"),$(cat "$base/operstate"),$(cat "$base/mtu"),netdev,,0,0"
    fi
  else
    echo "NET=$nic,0,0,0,0,unavailable,0,unavailable,,0,0"
  fi
done
"""


def parse_gpu_csv(raw: str) -> dict[str, Any]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 9:
        return {"available": False, "error": "unexpected nvidia-smi output"}
    used = finite_float(parts[7])
    total = finite_float(parts[8])
    return {
        "available": True,
        "name": parts[0],
        "temperature_c": finite_float(parts[1]),
        "power_w": finite_float(parts[2]),
        "sm_clock_mhz": finite_float(parts[3]),
        "graphics_clock_mhz": finite_float(parts[4]),
        "gpu_util_percent": finite_float(parts[5]),
        "memory_util_percent": finite_float(parts[6]),
        "framebuffer_used_bytes": used * 1024 * 1024 if used is not None else None,
        "framebuffer_total_bytes": total * 1024 * 1024 if total is not None else None,
    }


def parse_remote_probe(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {"network": {}, "gpu": {"available": False}}
    memory: dict[str, int] = {}
    thermal_samples: dict[str, list[float]] = defaultdict(list)
    for line in text.splitlines():
        if line.startswith("HOSTNAME="):
            result["hostname"] = line.split("=", 1)[1]
        elif line.startswith("GPU="):
            result["gpu"] = parse_gpu_csv(line.split("=", 1)[1])
        elif line.startswith("MEM_"):
            key, value = line.split("=", 1)
            memory[key[4:].rstrip(":")] = int(float(value))
        elif line.startswith("VLLM_RSS="):
            result["vllm_rss_bytes"] = int(line.split("=", 1)[1])
        elif line.startswith("THERMAL="):
            parts = line.split("=", 1)[1].rsplit(",", 1)
            if len(parts) == 2:
                name = parts[0].rsplit(".", 1)[-1].upper()
                value = millidegree_c(parts[1])
                if value is not None:
                    thermal_samples[name].append(value)
        elif line.startswith("NET="):
            parts = line.split("=", 1)[1].split(",")
            if len(parts) in (7, 11):
                values = {
                    "rx_bytes": int(parts[1]),
                    "tx_bytes": int(parts[2]),
                    "rx_errors": int(parts[3]),
                    "tx_errors": int(parts[4]),
                    "operstate": parts[5],
                    "mtu": int(parts[6]),
                }
                if len(parts) == 11:
                    values.update(
                        {
                            "counter_source": parts[7],
                            "rdma_device": parts[8] or None,
                            "rx_packets": int(parts[9]),
                            "tx_packets": int(parts[10]),
                        }
                    )
                result["network"][parts[0]] = values
    result["memory"] = {
        "total_bytes": memory.get("MemTotal"),
        "available_bytes": memory.get("MemAvailable"),
        "used_bytes": (
            memory["MemTotal"] - memory["MemAvailable"]
            if "MemTotal" in memory and "MemAvailable" in memory
            else None
        ),
        "swap_used_bytes": (
            memory["SwapTotal"] - memory["SwapFree"]
            if "SwapTotal" in memory and "SwapFree" in memory
            else None
        ),
    }
    result["thermals"] = summarize_thermals(thermal_samples)
    return result


class Collector:
    def __init__(
        self,
        spark2_host: str,
        ssh_key: str,
        node_urls: dict[str, str],
        node_roles: dict[str, str],
        inference_mode: str,
        router_url: str,
        router_metrics_url: str,
        interfaces: tuple[str, ...],
        interval: float,
        spark1_host: str | None = None,
        spark1_ssh_key: str | None = None,
        ssh_known_hosts: str | None = None,
        ssh_control_dir: str | None = None,
        remote_interval: float = 30.0,
    ) -> None:
        self.spark1_host = spark1_host
        self.spark1_ssh_key = spark1_ssh_key or ssh_key
        self.spark2_host = spark2_host
        self.ssh_key = ssh_key
        self.ssh_known_hosts = ssh_known_hosts
        self.ssh_control_dir = self._prepare_ssh_control_dir(ssh_control_dir)
        self.node_urls = canonical_node_mapping(node_urls)
        self.node_roles = canonical_node_mapping(node_roles)
        missing_urls = set(CANONICAL_NODE_NAMES).difference(self.node_urls)
        missing_roles = set(CANONICAL_NODE_NAMES).difference(self.node_roles)
        if missing_urls or missing_roles:
            raise ValueError(
                "Dashboard configuration must define both cerberus1 and "
                "cerberus2 (legacy spark1/spark2 map keys are accepted)."
            )
        self.inference_mode = inference_mode
        self.router_url = router_url.rstrip("/")
        self.router_metrics_url = router_metrics_url.rstrip("/")
        self.interfaces = interfaces
        self.interval = interval
        self.remote_interval = max(remote_interval, interval)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.snapshot: dict[str, Any] = {
            "generated_at": None,
            "collector": {"state": "starting"},
            "nodes": {},
            "router": {"healthy": False, "state": "starting"},
            "cluster": {
                "state": "down",
                "healthy": False,
                "endpoint_healthy": False,
                "affected_nodes": [],
                "reason": "Waiting for the first health sample.",
                "outage_started_at": None,
                "outage_elapsed_seconds": 0.0,
                "recovery_started_at": None,
                "endpoint": {
                    "healthy": False,
                    "state": "starting",
                    "url": None,
                    "reason": "Waiting for the first health sample.",
                },
            },
        }
        self.previous_node_counters: dict[str, dict[str, Any]] = {}
        self.previous_router_counters: dict[str, Any] = {}
        self.history_limit = max(2, math.ceil(180 / self.interval))
        self.history: list[dict[str, Any]] = []
        self._outage_started_at: float | None = None
        self._outage_reason: str | None = None
        self._outage_affected_nodes: list[str] = []
        self._recovery_started_at: float | None = None
        self._recovery_healthy_samples = 0
        self._remote_last_attempt: dict[str, float] = {}
        self._remote_last_sample: dict[str, float] = {}
        self._remote_counter_baselines: dict[str, tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _prepare_ssh_control_dir(value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute() or len(str(path)) > 72:
            raise ValueError("SSH control directory must be a short absolute path")
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_gid != os.getgid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise ValueError("SSH control directory must be private and service-owned")
        return path

    def fetch_url(self, url: str, timeout: float = 1.5) -> tuple[int, str]:
        """Fetch a bounded endpoint under a real wall-clock deadline."""
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("dashboard endpoint URL is invalid")
        connection_type = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        connection = connection_type(parsed.hostname, port, timeout=timeout)
        started = time.monotonic()
        timer: threading.Timer | None = None
        try:
            connection.connect()
            live_socket = connection.sock
            if live_socket is None:
                raise OSError("dashboard endpoint did not create a socket")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("dashboard endpoint exceeded its deadline")

            def abort() -> None:
                try:
                    live_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(remaining, abort)
            timer.daemon = True
            timer.start()
            target = urllib.parse.urlunsplit(
                ("", "", parsed.path or "/", parsed.query, "")
            )
            connection.request(
                "GET", target, headers={"User-Agent": "dgx-spark-dashboard/1.0"}
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and int(length) > MAX_HTTP_RESPONSE_BYTES:
                raise ValueError("dashboard endpoint response is too large")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(
                    min(64 * 1024, MAX_HTTP_RESPONSE_BYTES + 1 - total)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_HTTP_RESPONSE_BYTES:
                    raise ValueError("dashboard endpoint response is too large")
            if time.monotonic() - started > timeout:
                raise TimeoutError("dashboard endpoint exceeded its deadline")
            return response.status, b"".join(chunks).decode("utf-8", "replace")
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    def local_system(self) -> dict[str, Any]:
        mem = read_meminfo()
        total = mem.get("MemTotal")
        available = mem.get("MemAvailable")
        return {
            "hostname": os.uname().nodename,
            "gpu": gpu_stats(),
            "thermals": thermal_stats(),
            "memory": {
                "total_bytes": total,
                "available_bytes": available,
                "used_bytes": (
                    total - available
                    if total is not None and available is not None
                    else None
                ),
                "swap_used_bytes": (
                    mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
                ),
            },
            "vllm_rss_bytes": vllm_rss_bytes(),
            "network": network_counters(self.interfaces),
        }

    def ssh_system(self, host: str, ssh_key: str) -> dict[str, Any]:
        """Collect one node through a non-interactive, host-key-verified SSH probe."""
        script = REMOTE_PROBE.replace("__INTERFACES__", " ".join(self.interfaces))
        command = [
            "ssh",
            "-i",
            ssh_key,
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
        if self.ssh_known_hosts:
            command.extend(
                ["-o", f"UserKnownHostsFile={self.ssh_known_hosts}"]
            )
        if self.ssh_control_dir is not None:
            identity = "\0".join(
                (host, ssh_key, self.ssh_known_hosts or "default-known-hosts")
            ).encode("utf-8")
            control_name = "ssh-" + hashlib.sha256(identity).hexdigest()[:24]
            command.extend(
                [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    "ControlPersist=60",
                    "-o",
                    f"ControlPath={self.ssh_control_dir / control_name}",
                ]
            )
        command.extend(
            [
                "-o",
                "ConnectTimeout=2",
                "-o",
                "ServerAliveInterval=2",
                host,
                "sh -s",
            ]
        )
        try:
            result = subprocess.run(
                command,
                input=script,
                capture_output=True,
                text=True,
                timeout=4.5,
                check=False,
            )
            if result.returncode:
                return {
                    "hostname": host,
                    "error": result.stderr.strip() or f"ssh exited {result.returncode}",
                }
            return parse_remote_probe(result.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            return {"hostname": host, "error": str(exc)}

    def spark1_system(self) -> dict[str, Any]:
        """Sample Cerberus 1 locally or remotely (compatibility method name)."""
        if not self.spark1_host:
            return self.local_system()
        return self.ssh_system(self.spark1_host, self.spark1_ssh_key)

    def remote_system(self) -> dict[str, Any]:
        """Sample Cerberus 2 over SSH (compatibility entry point)."""
        return self.ssh_system(self.spark2_host, self.ssh_key)

    def remote_probe_due(self, name: str, now: float | None = None) -> bool:
        """Return whether a node needs a new SSH sample."""
        observed = time.monotonic() if now is None else now
        previous = self._remote_last_attempt.get(name)
        return previous is None or observed - previous >= self.remote_interval

    def vllm_metrics(self, base_url: str) -> dict[str, Any]:
        started = time.monotonic()
        try:
            status, text = self.fetch_url(f"{base_url.rstrip('/')}/metrics")
            if status != 200:
                return {
                    "healthy": False,
                    "state": f"metrics HTTP {status}",
                    "latency_ms": (time.monotonic() - started) * 1000,
                }
            metrics = parse_prometheus(text)
            prompt = metric_sum(
                metrics, "vllm:prompt_tokens_total", "vllm_prompt_tokens_total"
            )
            generation = metric_sum(
                metrics,
                "vllm:generation_tokens_total",
                "vllm_generation_tokens_total",
            )
            accepted = metric_sum(
                metrics,
                "vllm:spec_decode_num_accepted_tokens_total",
                "vllm_spec_decode_num_accepted_tokens_total",
            )
            drafted = metric_sum(
                metrics,
                "vllm:spec_decode_num_draft_tokens_total",
                "vllm_spec_decode_num_draft_tokens_total",
            )
            prefix_hits = metric_sum(
                metrics,
                "vllm:prefix_cache_hits_total",
                "vllm_prefix_cache_hits_total",
            )
            prefix_queries = metric_sum(
                metrics,
                "vllm:prefix_cache_queries_total",
                "vllm_prefix_cache_queries_total",
            )
            return {
                "healthy": True,
                "state": "serving",
                "latency_ms": (time.monotonic() - started) * 1000,
                "running_requests": metric_sum(
                    metrics, "vllm:num_requests_running", "vllm_num_requests_running"
                ),
                "waiting_requests": metric_sum(
                    metrics, "vllm:num_requests_waiting", "vllm_num_requests_waiting"
                ),
                "kv_cache_usage_percent": (
                    lambda value: value * 100 if value is not None else None
                )(
                    metric_avg(
                        metrics,
                        "vllm:kv_cache_usage_perc",
                        "vllm_kv_cache_usage_perc",
                        "vllm:gpu_cache_usage_perc",
                        "vllm_gpu_cache_usage_perc",
                    )
                ),
                "counters": {
                    "prompt_tokens": prompt,
                    "generation_tokens": generation,
                    "accepted_tokens": accepted,
                    "draft_tokens": drafted,
                    "prefix_hits": prefix_hits,
                    "prefix_queries": prefix_queries,
                    "requests": metric_sum(
                        metrics,
                        "vllm:request_success_total",
                        "vllm_request_success_total",
                        "vllm:e2e_request_latency_seconds_count",
                        "vllm_e2e_request_latency_seconds_count",
                    ),
                },
                "dflash_acceptance_percent": (
                    accepted / drafted * 100
                    if accepted is not None and drafted is not None and drafted > 0
                    else (
                        lambda rate: (
                            rate * 100
                            if rate is not None and rate <= 1
                            else rate
                        )
                    )(
                        metric_avg(
                            metrics,
                            "vllm:spec_decode_draft_acceptance_rate",
                            "vllm_spec_decode_draft_acceptance_rate",
                        )
                    )
                ),
                "prefix_hit_percent": (
                    prefix_hits / prefix_queries * 100
                    if prefix_hits is not None
                    and prefix_queries is not None
                    and prefix_queries > 0
                    else None
                ),
            }
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            return {
                "healthy": False,
                "state": "unreachable",
                "error": str(exc),
                "latency_ms": (time.monotonic() - started) * 1000,
            }

    def router_metrics(self) -> dict[str, Any]:
        started = time.monotonic()
        health_status: int | None = None
        health_error: str | None = None
        for path in ("/health", "/healthz"):
            try:
                health_status, _ = self.fetch_url(f"{self.router_url}{path}", 1.0)
                if health_status < 500:
                    break
            except (OSError, urllib.error.URLError, TimeoutError) as exc:
                health_error = str(exc)
        try:
            status, text = self.fetch_url(f"{self.router_metrics_url}/metrics", 1.2)
            metrics = parse_prometheus(text) if status == 200 else {}
        except (OSError, urllib.error.URLError, TimeoutError):
            metrics = {}
        requests = metric_sum(
            metrics,
            "vllm_router_requests_total",
            "vllm_router_processed_requests_total",
            "vllm_router_policy_decisions_total",
            "router_requests_total",
            "http_requests_total",
            "request_count_total",
        )
        healthy = health_status is not None and 200 <= health_status < 400
        return {
            "healthy": healthy,
            "state": (
                "routing"
                if healthy
                else ("starting" if metrics else "unreachable")
            ),
            "url": self.router_url,
            "metrics_url": self.router_metrics_url,
            "latency_ms": (time.monotonic() - started) * 1000,
            "counters": {"requests": requests},
            "active_workers": metric_sum(metrics, "vllm_router_active_workers"),
            **({"error": health_error} if not healthy and health_error else {}),
        }

    @staticmethod
    def worker_state(system: dict[str, Any]) -> dict[str, Any]:
        """Represent a headless TP rank without probing a nonexistent HTTP API."""
        system_error = system.get("error")
        rss = system.get("vllm_rss_bytes")
        process_running = rss is not None and rss > 0
        if system_error:
            state = "unreachable"
        elif process_running:
            state = "headless_worker"
        else:
            state = "worker_stopped"
        return {
            "healthy": not system_error and process_running,
            "state": state,
            "role": "worker",
            "headless": True,
            "metrics_scope": "none",
            "counters": {},
            "rates": {},
            "dflash_window_acceptance_percent": None,
            **({"error": str(system_error)} if system_error else {}),
        }

    @staticmethod
    def node_health(name: str, node: dict[str, Any]) -> dict[str, Any]:
        """Summarize service and telemetry health without hiding TP failures."""
        role = node.get("role", "replica")
        system = node.get("system", {})
        vllm = node.get("vllm", {})
        system_error = system.get("error")
        service_healthy = bool(vllm.get("healthy"))
        service_state = readable_state(vllm.get("state"))
        service_error = vllm.get("error")

        if role == "worker":
            if system_error:
                reason = f"{name} cannot be reached over SSH: {system_error}"
            elif not service_healthy:
                reason = f"Required TP worker {name} is {service_state}."
            else:
                reason = None
            state = vllm.get("state", "unreachable")
            healthy = service_healthy
        elif not service_healthy:
            detail = f": {service_error}" if service_error else "."
            reason = (
                f"{name} {readable_state(role)} endpoint is "
                f"{service_state}{detail}"
            )
            state = vllm.get("state", "unreachable")
            healthy = False
        elif system_error:
            reason = f"{name} telemetry cannot be reached over SSH: {system_error}"
            state = "telemetry_unreachable"
            healthy = False
        else:
            reason = None
            state = vllm.get("state", "serving")
            healthy = True

        return {
            "healthy": healthy,
            "state": state,
            "reason": reason,
        }

    @staticmethod
    def endpoint_health(endpoint: dict[str, Any]) -> dict[str, Any]:
        """Expose a compact endpoint record that is stable across topologies."""
        healthy = bool(endpoint.get("healthy"))
        state = endpoint.get("state", "unreachable")
        reason = None
        if not healthy:
            label = endpoint.get("label") or "Inference endpoint"
            error = endpoint.get("error")
            detail = f": {error}" if error else "."
            reason = f"{label} is {readable_state(state)}{detail}"
        return {
            "healthy": healthy,
            "state": state,
            "url": endpoint.get("url"),
            "reason": reason,
        }

    @classmethod
    def observed_cluster_health(
        cls, nodes: dict[str, Any], endpoint: dict[str, Any]
    ) -> dict[str, Any]:
        """Infer the current cluster state before applying recovery hysteresis."""
        endpoint_summary = cls.endpoint_health(endpoint)
        affected_nodes: list[str] = []
        node_reasons: list[str] = []
        for name, node in nodes.items():
            health = node.get("health") or cls.node_health(name, node)
            if not health.get("healthy"):
                affected_nodes.append(name)
                if health.get("reason"):
                    node_reasons.append(str(health["reason"]))

        tp_workers_down = [
            name
            for name, node in nodes.items()
            if node.get("role") == "worker"
            and not (node.get("health") or cls.node_health(name, node)).get(
                "healthy"
            )
        ]
        active_ranks = endpoint.get("active_ranks")
        expected_ranks = endpoint.get("expected_ranks")
        incomplete_tp = (
            endpoint.get("mode") == "direct"
            and active_ranks is not None
            and expected_ranks is not None
            and active_ranks < expected_ranks
        )
        router_has_no_workers = (
            endpoint.get("mode") == "router"
            and endpoint.get("active_workers") is not None
            and endpoint.get("active_workers") <= 0
        )
        serving_nodes = [
            node
            for node in nodes.values()
            if node.get("role") != "worker"
            and bool(node.get("vllm", {}).get("healthy"))
        ]

        critical_reasons: list[str] = []
        if not endpoint_summary["healthy"]:
            critical_reasons.append(str(endpoint_summary["reason"]))
        if tp_workers_down:
            critical_reasons.extend(
                str(
                    (nodes[name].get("health") or cls.node_health(name, nodes[name]))[
                        "reason"
                    ]
                )
                for name in tp_workers_down
            )
        if incomplete_tp and not tp_workers_down:
            critical_reasons.append(
                f"Only {active_ranks} of {expected_ranks} required TP ranks are active."
            )
        if router_has_no_workers:
            critical_reasons.append("Router reports no active inference workers.")
        if endpoint_summary["healthy"] and not serving_nodes:
            critical_reasons.append("No serving model endpoint is reachable.")

        if critical_reasons:
            state = "down"
            reason = " ".join(dict.fromkeys(critical_reasons))
        elif affected_nodes:
            state = "degraded"
            reason = " ".join(dict.fromkeys(node_reasons))
        else:
            state = "serving"
            reason = (
                f"Inference endpoint and all {len(nodes)} nodes are healthy."
            )

        return {
            "state": state,
            "healthy": state == "serving",
            "endpoint_healthy": endpoint_summary["healthy"],
            "affected_nodes": affected_nodes,
            "reason": reason,
            "endpoint": endpoint_summary,
        }

    def cluster_health(
        self,
        nodes: dict[str, Any],
        endpoint: dict[str, Any],
        now: float,
    ) -> dict[str, Any]:
        """Apply persistent incident timing and a short recovery hysteresis."""
        observed = self.observed_cluster_health(nodes, endpoint)
        state = observed["state"]

        if state != "serving":
            if self._outage_started_at is None:
                self._outage_started_at = now
                self._outage_affected_nodes = []
            self._outage_reason = observed["reason"]
            for name in observed["affected_nodes"]:
                if name not in self._outage_affected_nodes:
                    self._outage_affected_nodes.append(name)
            self._recovery_started_at = None
            self._recovery_healthy_samples = 0
        elif self._outage_started_at is not None:
            if self._recovery_started_at is None:
                self._recovery_started_at = now
            self._recovery_healthy_samples += 1
            if self._recovery_healthy_samples <= RECOVERY_HEALTHY_SAMPLES:
                observed["state"] = "recovering"
                observed["healthy"] = True
                observed["affected_nodes"] = list(self._outage_affected_nodes)
                observed["reason"] = (
                    "Inference is responding again; confirming stability "
                    f"({self._recovery_healthy_samples}/"
                    f"{RECOVERY_HEALTHY_SAMPLES} healthy samples). "
                    f"Previous issue: {self._outage_reason}"
                )
            else:
                self._outage_started_at = None
                self._outage_reason = None
                self._outage_affected_nodes = []
                self._recovery_started_at = None
                self._recovery_healthy_samples = 0

        if self._outage_started_at is not None:
            end = self._recovery_started_at or now
            observed["outage_started_at"] = utc_timestamp(
                self._outage_started_at
            )
            observed["outage_elapsed_seconds"] = round(
                max(0.0, end - self._outage_started_at), 1
            )
            observed["recovery_started_at"] = (
                utc_timestamp(self._recovery_started_at)
                if self._recovery_started_at is not None
                else None
            )
        else:
            observed["outage_started_at"] = None
            observed["outage_elapsed_seconds"] = None
            observed["recovery_started_at"] = None
        return observed

    @staticmethod
    def metric_source_names(nodes: dict[str, Any]) -> list[str]:
        """Return non-overlapping vLLM metric sources for cluster totals.

        An aggregate endpoint already contains the work of every TP rank, so it
        takes precedence over any independently configured replica.  Workers
        never contribute API counters.
        """
        aggregates = [
            name
            for name, node in nodes.items()
            if node.get("role") == "aggregate"
        ]
        if aggregates:
            return aggregates
        return [
            name
            for name, node in nodes.items()
            if node.get("role") == "replica"
        ]

    @classmethod
    def add_backend_rates(
        cls, endpoint: dict[str, Any], nodes: dict[str, Any]
    ) -> None:
        sources = cls.metric_source_names(nodes)
        endpoint["metrics_source_nodes"] = sources
        for metric in ("prompt_tokens", "generation_tokens"):
            values = [
                nodes[name]["vllm"].get("rates", {}).get(
                    f"{metric}_per_second"
                )
                for name in sources
            ]
            present = [value for value in values if value is not None]
            endpoint[f"backend_{metric}_per_second"] = (
                sum(present) if present else None
            )

    @classmethod
    def direct_endpoint(cls, nodes: dict[str, Any]) -> dict[str, Any]:
        """Build the top-level status for a rank-0 aggregate HTTP endpoint."""
        sources = cls.metric_source_names(nodes)
        source_name = sources[0] if sources else None
        source = nodes.get(source_name, {}) if source_name else {}
        vllm = source.get("vllm", {})
        active_ranks = sum(
            1 for node in nodes.values() if node.get("vllm", {}).get("healthy")
        )
        endpoint = {
            "healthy": bool(vllm.get("healthy")),
            "state": vllm.get("state", "unreachable"),
            "mode": "direct",
            "label": "TP2 aggregate endpoint",
            "url": source.get("endpoint"),
            "latency_ms": vllm.get("latency_ms"),
            "counters": {
                "requests": vllm.get("counters", {}).get("requests")
            },
            "rates": {
                "requests_per_second": vllm.get("rates", {}).get(
                    "requests_per_second"
                )
            },
            "active_ranks": active_ranks,
            "expected_ranks": len(nodes),
        }
        if not endpoint["healthy"] and vllm.get("error"):
            endpoint["error"] = vllm["error"]
        cls.add_backend_rates(endpoint, nodes)
        return endpoint

    @staticmethod
    def add_rates(
        current: dict[str, Any],
        previous: dict[str, Any] | None,
        elapsed: float,
    ) -> None:
        counters = current.get("counters", {})
        old = (previous or {}).get("counters", {})
        rates = {}
        for key, value in counters.items():
            rates[f"{key}_per_second"] = counter_rate(value, old.get(key), elapsed)
        current["rates"] = rates
        accepted_delta = (
            counters.get("accepted_tokens") - old.get("accepted_tokens")
            if counters.get("accepted_tokens") is not None
            and old.get("accepted_tokens") is not None
            and counters.get("accepted_tokens") >= old.get("accepted_tokens")
            else None
        )
        draft_delta = (
            counters.get("draft_tokens") - old.get("draft_tokens")
            if counters.get("draft_tokens") is not None
            and old.get("draft_tokens") is not None
            and counters.get("draft_tokens") >= old.get("draft_tokens")
            else None
        )
        current["dflash_window_acceptance_percent"] = (
            accepted_delta / draft_delta * 100
            if accepted_delta is not None and draft_delta is not None and draft_delta > 0
            else None
        )

    @staticmethod
    def add_network_rates(
        system: dict[str, Any],
        previous: dict[str, Any] | None,
        elapsed: float,
    ) -> None:
        previous_network = (previous or {}).get("network", {})
        for name, values in system.get("network", {}).items():
            old = previous_network.get(name, {})
            values["rx_bytes_per_second"] = counter_rate(
                values.get("rx_bytes"), old.get("rx_bytes"), elapsed
            )
            values["tx_bytes_per_second"] = counter_rate(
                values.get("tx_bytes"), old.get("tx_bytes"), elapsed
            )

    def collect(self) -> None:
        now = time.time()
        monotonic_now = time.monotonic()
        with self.lock:
            prior_snapshot = self.snapshot
        prior_time = prior_snapshot.get("_sample_time") or now - self.interval
        elapsed = max(now - prior_time, 0.001)

        tasks: dict[str, Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            if not self.spark1_host or self.remote_probe_due(
                "cerberus1", monotonic_now
            ):
                tasks["cerberus1_system"] = executor.submit(self.spark1_system)
                if self.spark1_host:
                    self._remote_last_attempt["cerberus1"] = monotonic_now
            if self.remote_probe_due("cerberus2", monotonic_now):
                tasks["cerberus2_system"] = executor.submit(self.remote_system)
                self._remote_last_attempt["cerberus2"] = monotonic_now
            if self.inference_mode == "router":
                tasks["router"] = executor.submit(self.router_metrics)
            for name, url in self.node_urls.items():
                if self.node_roles[name] != "worker":
                    tasks[f"{name}_vllm"] = executor.submit(self.vllm_metrics, url)
            values = {key: future.result() for key, future in tasks.items()}

        nodes: dict[str, Any] = {}
        for name in CANONICAL_NODE_NAMES:
            system_key = f"{name}_system"
            fresh_system = system_key in values
            if fresh_system:
                system = values[system_key]
                if name == "cerberus2" or self.spark1_host:
                    self._remote_last_sample[name] = now
                    system["sampled_at"] = utc_timestamp(now)
                    system["sample_age_seconds"] = 0.0
            else:
                system = copy.deepcopy(
                    prior_snapshot.get("nodes", {}).get(name, {}).get("system", {})
                )
                if not system:
                    system = {
                        "hostname": name,
                        "error": "remote sample unavailable",
                    }
                sampled = self._remote_last_sample.get(name)
                if sampled is not None:
                    system["sample_age_seconds"] = max(0.0, now - sampled)
            role = self.node_roles[name]
            if role == "worker":
                vllm = self.worker_state(system)
            else:
                vllm = values[f"{name}_vllm"]
                vllm["role"] = role
                vllm["headless"] = False
                vllm["metrics_scope"] = (
                    "cluster" if role == "aggregate" else "node"
                )
            previous_node = prior_snapshot.get("nodes", {}).get(name, {})
            is_remote = name == "cerberus2" or bool(self.spark1_host)
            if fresh_system and is_remote:
                baseline = self._remote_counter_baselines.get(name)
                baseline_elapsed = max(now - baseline[0], 0.001) if baseline else elapsed
                self.add_network_rates(
                    system, baseline[1] if baseline else None, baseline_elapsed
                )
                if system.get("network"):
                    self._remote_counter_baselines[name] = (now, copy.deepcopy(system))
            elif fresh_system:
                self.add_network_rates(system, previous_node.get("system"), elapsed)
            if role != "worker":
                self.add_rates(vllm, previous_node.get("vllm"), elapsed)
            node = {
                "label": name,
                "rank": 0 if name == "cerberus1" else 1,
                "role": role,
                "endpoint": (
                    self.node_urls[name] if role != "worker" else None
                ),
                "system": system,
                "vllm": vllm,
            }
            node["health"] = self.node_health(name, node)
            nodes[name] = node

        if self.inference_mode == "router":
            router = values["router"]
            router["mode"] = "router"
            router["label"] = "Router"
            previous_router = prior_snapshot.get("router")
            self.add_rates(router, previous_router, elapsed)
            self.add_backend_rates(router, nodes)
        else:
            router = self.direct_endpoint(nodes)
        cluster = self.cluster_health(nodes, router, now)
        snapshot = {
            "_sample_time": now,
            "generated_at": utc_timestamp(now),
            "sample_interval_seconds": elapsed,
            "collector": {
                "state": "ok",
                "interval_seconds": self.interval,
                "remote_interval_seconds": self.remote_interval,
            },
            "nodes": nodes,
            "router": router,
            "cluster": cluster,
        }
        history_point = {
            "generated_at": snapshot["generated_at"],
            "generation_tokens_per_second": router.get(
                "backend_generation_tokens_per_second"
            ),
            "nodes": {
                name: {
                    "gpu_c": node.get("system", {})
                    .get("gpu", {})
                    .get("temperature_c"),
                    "cpu_cluster_max_c": node.get("system", {})
                    .get("thermals", {})
                    .get("cpu_cluster_max_c"),
                    "soc_c": node.get("system", {})
                    .get("thermals", {})
                    .get("soc_c"),
                }
                for name, node in nodes.items()
            },
        }
        with self.lock:
            self.snapshot = snapshot
            self.history.append(history_point)
            if len(self.history) > self.history_limit:
                del self.history[: len(self.history) - self.history_limit]

    def run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.collect()
            except Exception as exc:  # keep dashboard alive after a transient probe bug
                with self.lock:
                    self.snapshot["collector"] = {"state": "error", "error": str(exc)}
            delay = max(0.2, self.interval - (time.monotonic() - started))
            self.stop_event.wait(delay)

    def get_snapshot(self) -> dict[str, Any]:
        with self.lock:
            result = copy.deepcopy(self.snapshot)
            result["history"] = copy.deepcopy(self.history)
        result.pop("_sample_time", None)
        return result


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "DGXSparkDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def is_authorized(self) -> bool:
        expected = getattr(self.server, "dashboard_auth", "")
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            actual = base64.b64decode(header[6:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(actual, expected)

    def require_auth(self) -> bool:
        if self.is_authorized():
            return False
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="DGX Spark dashboard"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return True

    def do_GET(self) -> None:  # noqa: N802
        if self.require_auth():
            return
        if self.path.split("?", 1)[0] == "/api/status":
            body = json.dumps(
                getattr(self.server, "collector").get_snapshot(),
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Successful browser telemetry polls are routine, not operator events.
        # Keep errors and other requests visible without growing journald every
        # two seconds while the dashboard is open.
        if self.path == "/api/status" and len(args) > 1 and str(args[1]) == "200":
            return
        print(
            f"{self.client_address[0]} - [{self.log_date_time_string()}] {fmt % args}",
            flush=True,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "8090"))
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("DASHBOARD_INTERVAL", "2")),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    auth = os.environ.get("DASHBOARD_AUTH", "")
    if args.host not in ("127.0.0.1", "::1", "localhost") and not auth:
        if os.environ.get("DASHBOARD_ALLOW_UNAUTHENTICATED") != "1":
            raise SystemExit(
                "Refusing a non-loopback bind without DASHBOARD_AUTH=user:password. "
                "Set credentials or explicitly set DASHBOARD_ALLOW_UNAUTHENTICATED=1."
            )
    interfaces = tuple(
        item.strip()
        for item in os.environ.get(
            "DASHBOARD_INTERFACES", ",".join(DEFAULT_INTERFACES)
        ).split(",")
        if item.strip()
    )
    node_roles = {
        "cerberus1": os.environ.get("SPARK1_VLLM_ROLE", "aggregate").strip().lower(),
        "cerberus2": os.environ.get("SPARK2_VLLM_ROLE", "worker").strip().lower(),
    }
    invalid_roles = {
        name: role
        for name, role in node_roles.items()
        if role not in VALID_NODE_ROLES
    }
    if invalid_roles:
        choices = ", ".join(sorted(VALID_NODE_ROLES))
        raise SystemExit(
            f"Invalid vLLM node role(s) {invalid_roles}; choose from {choices}."
        )
    inference_mode = os.environ.get(
        "DASHBOARD_INFERENCE_MODE", "direct"
    ).strip().lower()
    if inference_mode not in VALID_INFERENCE_MODES:
        choices = ", ".join(sorted(VALID_INFERENCE_MODES))
        raise SystemExit(
            f"Invalid DASHBOARD_INFERENCE_MODE={inference_mode!r}; "
            f"choose from {choices}."
        )
    default_ssh_key = str(Path.home() / ".ssh" / "id_ed25519_dgx_cluster")
    spark2_ssh_key = os.environ.get("SPARK2_SSH_KEY", default_ssh_key)
    collector = Collector(
        spark2_host=os.environ.get("SPARK2_SSH_HOST", "cerberus2"),
        ssh_key=spark2_ssh_key,
        node_urls={
            "cerberus1": os.environ.get(
                "SPARK1_VLLM_URL", "http://127.0.0.1:8000"
            ),
            "cerberus2": os.environ.get(
                "SPARK2_VLLM_URL", "http://cerberus2.local:8000"
            ),
        },
        node_roles=node_roles,
        inference_mode=inference_mode,
        router_url=os.environ.get("VLLM_ROUTER_URL", "http://127.0.0.1:8080"),
        router_metrics_url=os.environ.get(
            "VLLM_ROUTER_METRICS_URL", "http://127.0.0.1:29000"
        ),
        interfaces=interfaces,
        interval=max(args.interval, 0.5),
        spark1_host=(
            os.environ.get("SPARK1_SSH_HOST", "").strip() or None
        ),
        spark1_ssh_key=os.environ.get("SPARK1_SSH_KEY", default_ssh_key),
        ssh_known_hosts=(
            os.environ.get("DASHBOARD_SSH_KNOWN_HOSTS", "").strip() or None
        ),
        ssh_control_dir=(
            os.environ.get("DASHBOARD_SSH_CONTROL_DIR", "").strip() or None
        ),
        remote_interval=max(
            float(os.environ.get("DASHBOARD_REMOTE_INTERVAL", "30")), args.interval
        ),
    )
    thread = threading.Thread(target=collector.run, name="collector", daemon=True)
    thread.start()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.collector = collector  # type: ignore[attr-defined]
    server.dashboard_auth = auth  # type: ignore[attr-defined]
    print(f"DGX Spark dashboard listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        collector.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
