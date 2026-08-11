#!/usr/bin/env python3
"""Small, read-only cluster metrics server for the Cerberus kiosk.

The collector has no third-party dependencies.  It samples the local host
directly, samples the other configured hosts over the existing cluster SSH
trust, and derives live generation-token throughput from the cumulative vLLM
counter exported by the production endpoint.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime
import json
import math
import os
import re
import socket
import stat
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
MAX_VOICE_STATUS_BYTES = 32 * 1024
DEFAULT_VOICE_STATUS_PATH = "/run/cerebrus3-voice-bridge/status.json"
DEFAULT_VOICE_STALE_SECONDS = 6.0
MAX_STATUS_TIMESTAMP = 253_402_300_799.0  # 9999-12-31T23:59:59Z
HOST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
STATUS_TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
GB10_CPU_THERMAL_ZONES = frozenset({"TS0E", "TS0P", "TS1E", "TS1P"})
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
  nvidia-smi --query-gpu=utilization.gpu,temperature.gpu \
    --format=csv,noheader,nounits 2>/dev/null |
    sed 's/^/GPU=/' || true
fi
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
  case "$driver" in
    jc42|spd5118) ;;
    *) continue ;;
  esac
  for input in "$hwmon"/temp*_input; do
    [ -r "$input" ] || continue
    thermal_value=$(cat "$input" 2>/dev/null || true)
    [ -n "$thermal_value" ] &&
      printf 'THERMAL=MEMORY,%s\n' "$thermal_value"
  done
done
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


def celsius(value: Any) -> float | None:
    """Accept only physically plausible direct Celsius sensor readings."""
    parsed = finite_float(value)
    if parsed is None or not -20 <= parsed <= 150:
        return None
    return round(parsed, 1)


def millidegree_c(value: Any) -> float | None:
    """Convert a plausible Linux thermal/hwmon millidegree value."""
    parsed = finite_float(value)
    if parsed is None or not -20_000 <= parsed <= 150_000:
        return None
    return round(parsed / 1000, 1)


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
        "gpu_temperature_c": None,
        "cpu_temperature_c": None,
        "soc_temperature_c": None,
        "ram_temperature_c": None,
        "memory_temperature_c": None,
        "memory_temperature_sensor_available": False,
        "ram_total_bytes": None,
        "ram_available_bytes": None,
    }
    gpu_values: list[float] = []
    gpu_temperatures: list[float] = []
    thermal_samples: dict[str, list[float]] = defaultdict(list)
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
            fields = line.split("=", 1)[1].split(",")
            value = finite_float(fields[0].strip())
            if value is not None and 0 <= value <= 100:
                gpu_values.append(value)
            if len(fields) >= 2:
                temperature = celsius(fields[1].strip())
                if temperature is not None:
                    gpu_temperatures.append(temperature)
        elif line.startswith("THERMAL="):
            fields = line.split("=", 1)[1].rsplit(",", 1)
            if len(fields) != 2:
                continue
            name = fields[0].rsplit(".", 1)[-1].upper()
            temperature = millidegree_c(fields[1])
            if temperature is not None and name in {
                *GB10_CPU_THERMAL_ZONES,
                "TSOC",
                "TGPU",
                "MEMORY",
            }:
                thermal_samples[name].append(temperature)
    if gpu_values:
        result["gpu_percent"] = percent(fmean(gpu_values))
    if gpu_temperatures:
        # A Spark has one GB10 GPU, but max remains the safe definition if a
        # future nvidia-smi exposes more than one temperature row.
        result["gpu_temperature_c"] = max(gpu_temperatures)
    elif thermal_samples.get("TGPU"):
        result["gpu_temperature_c"] = max(thermal_samples["TGPU"])

    cpu_temperatures = [
        value
        for name in GB10_CPU_THERMAL_ZONES
        for value in thermal_samples.get(name, ())
    ]
    if cpu_temperatures:
        result["cpu_temperature_c"] = max(cpu_temperatures)
    if thermal_samples.get("TSOC"):
        result["soc_temperature_c"] = max(thermal_samples["TSOC"])
    if thermal_samples.get("MEMORY"):
        memory_temperature = max(thermal_samples["MEMORY"])
        # Both names are emitted deliberately: RAM matches the utilization
        # field/UI language, while memory is the hardware-neutral API name.
        result["ram_temperature_c"] = memory_temperature
        result["memory_temperature_c"] = memory_temperature
        result["memory_temperature_sensor_available"] = True
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


def timestamp_epoch(value: Any) -> float | None:
    """Parse an epoch or ISO-8601 timestamp without accepting booleans/NaN."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = finite_float(value)
        return (
            parsed
            if parsed is not None and 0 <= parsed <= MAX_STATUS_TIMESTAMP
            else None
        )
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    numeric = finite_float(text)
    if numeric is not None:
        return numeric if 0 <= numeric <= MAX_STATUS_TIMESTAMP else None
    try:
        parsed_datetime = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_datetime.tzinfo is None:
        parsed_datetime = parsed_datetime.replace(tzinfo=datetime.timezone.utc)
    parsed = finite_float(parsed_datetime.timestamp())
    return (
        parsed
        if parsed is not None and 0 <= parsed <= MAX_STATUS_TIMESTAMP
        else None
    )


def status_token(value: Any, default: str = "unknown") -> str:
    """Return a bounded, display-safe status token, never arbitrary content."""
    if not isinstance(value, str):
        return default
    token = value.strip()
    return token.lower() if STATUS_TOKEN_PATTERN.fullmatch(token) else default


def status_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def bounded_duration(value: Any) -> float | None:
    parsed = finite_float(value)
    if parsed is None or not 0 <= parsed <= 86_400:
        return None
    return round(parsed, 2)


class VoiceStatusReader:
    """Read and normalize the bridge heartbeat without exposing voice content."""

    ACTIVE_COMPONENT_STATES = frozenset(
        {"processing", "thinking", "synthesizing", "playing", "cooldown"}
    )

    def __init__(self, path: str, stale_after_seconds: float) -> None:
        candidate = Path(path)
        if not candidate.is_absolute():
            raise ValueError("voice status path must be absolute")
        self.path = candidate
        self.stale_after_seconds = max(5.0, stale_after_seconds)

    def _empty(self, state: str, error_code: str | None) -> dict[str, Any]:
        component = {
            "state": "unknown",
            "started_at": None,
            "completed_at": None,
            "last_success_at": None,
            "duration_seconds": None,
            "elapsed_seconds": None,
        }
        return {
            "schema": 1,
            "service": "cerberus-voice",
            "device": "Cerberus",
            "state": state,
            "healthy": False,
            "stage": "unknown",
            "stage_started_at": None,
            "stage_elapsed_seconds": None,
            "updated_at": None,
            "age_seconds": None,
            "stale_after_seconds": self.stale_after_seconds,
            "pid": None,
            "sequence": None,
            "watchword": {
                "state": "unknown",
                "last_triggered_at": None,
                "armed_until": None,
                "armed_remaining_seconds": None,
            },
            "asr": dict(component),
            "openclaw": dict(component),
            "tts": {**component, "chunk_index": None, "chunk_total": None},
            "last_error": None,
            "status_error": error_code,
        }

    def _read_json(self) -> tuple[dict[str, Any], float]:
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("not_regular")
            if metadata.st_size > MAX_VOICE_STATUS_BYTES:
                raise ValueError("too_large")
            chunks: list[bytes] = []
            remaining = MAX_VOICE_STATUS_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_VOICE_STATUS_BYTES:
                raise ValueError("too_large")
            decoded = json.loads(payload.decode("utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("not_object")
            return decoded, metadata.st_mtime
        finally:
            os.close(descriptor)

    @staticmethod
    def _public_timestamp(value: Any) -> str | None:
        parsed = timestamp_epoch(value)
        return utc_timestamp(parsed) if parsed is not None else None

    def _component(
        self,
        raw: Any,
        now: float,
        *,
        include_chunks: bool = False,
    ) -> dict[str, Any]:
        source = status_mapping(raw)
        started = timestamp_epoch(source.get("started_at"))
        completed = timestamp_epoch(source.get("completed_at"))
        last_success = timestamp_epoch(source.get("last_success_at"))
        state = status_token(source.get("state"))
        duration = bounded_duration(source.get("duration_seconds"))
        elapsed = None
        if state in self.ACTIVE_COMPONENT_STATES and started is not None:
            elapsed = round(max(0.0, now - started), 1)
        elif duration is not None:
            elapsed = duration
        result: dict[str, Any] = {
            "state": state,
            "started_at": utc_timestamp(started) if started is not None else None,
            "completed_at": utc_timestamp(completed) if completed is not None else None,
            "last_success_at": (
                utc_timestamp(last_success) if last_success is not None else None
            ),
            "duration_seconds": duration,
            "elapsed_seconds": elapsed,
        }
        if include_chunks:
            chunk_index = source.get("chunk_index")
            chunk_total = source.get("chunk_total")
            result["chunk_index"] = (
                chunk_index
                if isinstance(chunk_index, int)
                and not isinstance(chunk_index, bool)
                and 0 <= chunk_index <= 999
                else None
            )
            result["chunk_total"] = (
                chunk_total
                if isinstance(chunk_total, int)
                and not isinstance(chunk_total, bool)
                and 0 <= chunk_total <= 999
                else None
            )
        return result

    def read(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        try:
            raw, modified_at = self._read_json()
        except FileNotFoundError:
            return self._empty("down", "missing")
        except PermissionError:
            return self._empty("down", "unreadable")
        except json.JSONDecodeError:
            return self._empty("down", "malformed")
        except (OSError, UnicodeError, ValueError):
            return self._empty("down", "invalid")

        schema = raw.get("schema")
        if (
            isinstance(schema, bool)
            or schema != 1
            or raw.get("service") != "cerberus-voice"
        ):
            return self._empty("down", "schema_mismatch")

        overall = status_mapping(raw.get("overall"))
        wake_word = status_mapping(raw.get("wake_word"))
        updated = timestamp_epoch(raw.get("updated_at_epoch"))
        if updated is None:
            updated = timestamp_epoch(raw.get("updated_at"))
        if updated is None:
            updated = modified_at
        if updated > now + 5.0:
            return self._empty("down", "invalid")
        age = max(0.0, now - updated)
        state = status_token(overall.get("state"))
        stage = status_token(overall.get("stage"))
        stage_started = timestamp_epoch(overall.get("stage_started_at"))
        stale = age > self.stale_after_seconds
        status_clock = updated if stale else now
        healthy = state in {"ready", "busy", "armed"} and not stale

        armed_until = timestamp_epoch(wake_word.get("armed_until"))
        last_triggered = timestamp_epoch(wake_word.get("last_trigger_at"))
        last_error_source = status_mapping(raw.get("last_error"))
        last_error = None
        if last_error_source:
            error_stage = status_token(last_error_source.get("stage"))
            error_type = status_token(last_error_source.get("type"))
            error_at = timestamp_epoch(last_error_source.get("at"))
            last_error = {
                "stage": error_stage,
                "error_type": error_type,
                "at": utc_timestamp(error_at) if error_at is not None else None,
            }

        pid = raw.get("pid")
        sequence = raw.get("sequence")
        return {
            "schema": 1,
            "service": "cerberus-voice",
            "device": "Cerberus",
            "state": "stale" if stale else state,
            "healthy": healthy,
            "stage": stage,
            "stage_started_at": (
                utc_timestamp(stage_started) if stage_started is not None else None
            ),
            "stage_elapsed_seconds": (
                round(max(0.0, status_clock - stage_started), 1)
                if stage_started is not None
                else None
            ),
            "updated_at": utc_timestamp(updated),
            "age_seconds": round(age, 1),
            "stale_after_seconds": self.stale_after_seconds,
            "pid": (
                pid
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
                else None
            ),
            "sequence": (
                sequence
                if isinstance(sequence, int)
                and not isinstance(sequence, bool)
                and sequence >= 0
                else None
            ),
            "watchword": {
                "state": status_token(wake_word.get("state")),
                "last_triggered_at": (
                    utc_timestamp(last_triggered)
                    if last_triggered is not None
                    else None
                ),
                "armed_until": (
                    utc_timestamp(armed_until) if armed_until is not None else None
                ),
                "armed_remaining_seconds": (
                    round(max(0.0, armed_until - status_clock), 1)
                    if armed_until is not None
                    else None
                ),
            },
            "asr": self._component(raw.get("asr"), status_clock),
            "openclaw": self._component(raw.get("openclaw"), status_clock),
            "tts": self._component(
                raw.get("tts"), status_clock, include_chunks=True
            ),
            "last_error": last_error,
            "status_error": "stale" if stale else None,
        }


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
        voice_status_path: str = DEFAULT_VOICE_STATUS_PATH,
        voice_stale_after_seconds: float = DEFAULT_VOICE_STALE_SECONDS,
    ) -> None:
        self.nodes = validate_nodes(nodes)
        self.interval = max(1.0, interval)
        self.metrics_url = metrics_url
        self.history_points = max(2, history_points)
        self.host_prober = host_prober or HostProber(ssh_key, known_hosts)
        self.metrics_fetcher = metrics_fetcher
        self.voice_status_reader = VoiceStatusReader(
            voice_status_path, voice_stale_after_seconds
        )
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
                "cpu_temperature_c": None,
                "gpu_temperature_c": None,
                "soc_temperature_c": None,
                "ram_temperature_c": None,
                "memory_temperature_c": None,
                "memory_temperature_sensor_available": False,
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
                "cpu_temperature_c": None,
                "gpu_temperature_c": None,
                "soc_temperature_c": None,
                "ram_temperature_c": None,
                "memory_temperature_c": None,
                "sampled_hosts": {
                    "cpu": 0,
                    "gpu": 0,
                    "ram": 0,
                    "cpu_temperature": 0,
                    "gpu_temperature": 0,
                    "soc_temperature": 0,
                    "ram_temperature": 0,
                    "memory_temperature": 0,
                },
            },
            "throughput": self.throughput_tracker._base(time.time()),
            "voice_agent": self.voice_status_reader.read(time.time()),
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
                "cpu_temperature_c": None,
                "gpu_temperature_c": None,
                "soc_temperature_c": None,
                "ram_temperature_c": None,
                "memory_temperature_c": None,
                "memory_temperature_sensor_available": False,
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
        memory_temperature = celsius(raw.get("memory_temperature_c"))
        ram_temperature = celsius(raw.get("ram_temperature_c"))
        if memory_temperature is None:
            memory_temperature = ram_temperature
        if ram_temperature is None:
            ram_temperature = memory_temperature
        return {
            "name": name,
            "reported_hostname": raw.get("reported_hostname"),
            "state": "up",
            "cpu_percent": cpu,
            "gpu_percent": percent(finite_float(raw.get("gpu_percent"))),
            "ram_percent": ram,
            "cpu_temperature_c": celsius(raw.get("cpu_temperature_c")),
            "gpu_temperature_c": celsius(raw.get("gpu_temperature_c")),
            "soc_temperature_c": celsius(raw.get("soc_temperature_c")),
            "ram_temperature_c": ram_temperature,
            "memory_temperature_c": memory_temperature,
            "memory_temperature_sensor_available": memory_temperature is not None,
            "ram_used_bytes": ram_used,
            "ram_total_bytes": ram_total if isinstance(ram_total, int) else None,
            "sampled_at": utc_timestamp(now),
            "age_seconds": 0.0,
            "error": None,
        }

    def collect(self, now: float | None = None) -> None:
        fixed_test_time = now is not None
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
        cpu_temperature, cpu_temperature_count = average_field(
            hosts, "cpu_temperature_c"
        )
        gpu_temperature, gpu_temperature_count = average_field(
            hosts, "gpu_temperature_c"
        )
        soc_temperature, soc_temperature_count = average_field(
            hosts, "soc_temperature_c"
        )
        ram_temperature, ram_temperature_count = average_field(
            hosts, "ram_temperature_c"
        )
        memory_temperature, memory_temperature_count = average_field(
            hosts, "memory_temperature_c"
        )
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
            "cpu_temperature_c": cpu_temperature,
            "gpu_temperature_c": gpu_temperature,
            "soc_temperature_c": soc_temperature,
            "ram_temperature_c": ram_temperature,
            "memory_temperature_c": memory_temperature,
            "sampled_hosts": {
                "cpu": cpu_count,
                "gpu": gpu_count,
                "ram": ram_count,
                "cpu_temperature": cpu_temperature_count,
                "gpu_temperature": gpu_temperature_count,
                "soc_temperature": soc_temperature_count,
                "ram_temperature": ram_temperature_count,
                "memory_temperature": memory_temperature_count,
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
        # Real collection can spend several seconds waiting on remote probes;
        # use the current clock for this local heartbeat rather than the host
        # sample's earlier timestamp. Tests retain their explicit fixed clock.
        voice_agent = self.voice_status_reader.read(now if fixed_test_time else None)

        generated_at = utc_timestamp(now)
        history_point = {
            "timestamp": generated_at,
            "cluster": {
                "cpu_percent": cluster["cpu_percent"],
                "gpu_percent": cluster["gpu_percent"],
                "ram_percent": cluster["ram_percent"],
                "cpu_temperature_c": cluster["cpu_temperature_c"],
                "gpu_temperature_c": cluster["gpu_temperature_c"],
                "soc_temperature_c": cluster["soc_temperature_c"],
                "ram_temperature_c": cluster["ram_temperature_c"],
                "memory_temperature_c": cluster["memory_temperature_c"],
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
                    "cpu_temperature_c": host["cpu_temperature_c"],
                    "gpu_temperature_c": host["gpu_temperature_c"],
                    "soc_temperature_c": host["soc_temperature_c"],
                    "ram_temperature_c": host["ram_temperature_c"],
                    "memory_temperature_c": host["memory_temperature_c"],
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
                "voice_agent": voice_agent,
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

    def get_voice_status(self, now: float | None = None) -> dict[str, Any]:
        """Read the fast heartbeat independently of five-second host probes."""
        return self.voice_status_reader.read(now)


class DashboardHandler(SimpleHTTPRequestHandler):
    server_version = "C3ClusterDashboard/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in (
            "/api/voice-status",
            "/api/voice-status/",
            "/api/voice",
            "/api/voice/",
        ):
            body = json.dumps(
                getattr(self.server, "collector").get_voice_status(),
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
        voice_status_path=os.environ.get(
            "C3_DASHBOARD_VOICE_STATUS_PATH", DEFAULT_VOICE_STATUS_PATH
        ),
        voice_stale_after_seconds=float(
            os.environ.get(
                "C3_DASHBOARD_VOICE_STALE_SECONDS",
                str(DEFAULT_VOICE_STALE_SECONDS),
            )
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
