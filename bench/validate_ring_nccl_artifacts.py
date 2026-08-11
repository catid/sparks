#!/usr/bin/env python3
"""Validate per-node NCCL runtime reports and ring counter deltas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ring_rdma_counters import NETDEVS, RDMA_ERRORS, RDMA_HW_ERRORS, RDMAS


HCA_SELECTOR = "=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1"
CONTAINER_NCCL_PREFIX = (
    "/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/libnccl.so"
)


def named_path(value: str) -> tuple[str, Path]:
    name, separator, raw_path = value.partition("=")
    if not separator or name not in {"cerberus1", "cerberus2", "cerberus3"}:
        raise argparse.ArgumentTypeError("expected cerberusN=/path")
    return name, Path(raw_path)


def load_result(path: Path) -> tuple[dict, str]:
    matches = []
    transport_lines = []
    for line in path.read_text(errors="replace").splitlines():
        marker = "RING_NCCL_RESULT="
        if marker in line:
            matches.append(json.loads(line.split(marker, 1)[1]))
        else:
            transport_lines.append(line)
    if len(matches) != 1:
        raise RuntimeError(f"{path}: expected one runtime result, found {len(matches)}")
    return matches[0], "\n".join(transport_lines)


def validate_result(
    node: str, result: dict, transport_log: str, expected_runtime: int
) -> dict:
    rank = int(node.removeprefix("cerberus")) - 1
    problems = []
    expected_fields = {
        "schema": 1,
        "node": node,
        "rank": rank,
        "world_size": 3,
        "nccl_runtime_version": expected_runtime,
        "nccl_ib_hca": HCA_SELECTOR,
        "nccl_socket_ifname": "=enP7s7",
        "nccl_subnet_aware_routing": "1",
        "nccl_net_plugin": "none",
        "nccl_cross_nic": None,
    }
    for field, expected in expected_fields.items():
        if result.get(field) != expected:
            problems.append(f"{field}={result.get(field)!r}, expected={expected!r}")
    paths = result.get("loaded_nccl_paths")
    if not isinstance(paths, list) or not paths:
        problems.append("no mapped libnccl path was recorded")
    elif any(not str(path).startswith("/") for path in paths):
        problems.append(f"non-absolute libnccl path: {paths!r}")
    elif not any(str(path).startswith(CONTAINER_NCCL_PREFIX) for path in paths):
        problems.append(f"container NCCL wheel path was not mapped: {paths!r}")
    if result.get("observed_value") != result.get("expected_value"):
        problems.append("all-reduce result validation failed")
    if "NCCL INFO Using network IB" not in transport_log:
        problems.append("NCCL did not report the internal IB network")
    for hca in RDMAS:
        if hca not in transport_log:
            problems.append(f"NCCL log did not enumerate {hca}")
    if problems:
        raise RuntimeError(f"{node} runtime proof failed: {'; '.join(problems)}")
    return {
        "nccl_runtime_version": result["nccl_runtime_version"],
        "loaded_nccl_paths": paths,
        "payload_gib_per_second": result.get("payload_gib_per_second"),
    }


def validate_diff(node: str, diff: dict, min_hca_bytes: int) -> dict:
    if diff.get("schema") != 1 or diff.get("node") != node:
        raise RuntimeError(f"{node}: invalid counter-diff identity")
    if set(diff.get("rdma", {})) != set(RDMAS):
        raise RuntimeError(f"{node}: RDMA device set is incomplete")
    if set(diff.get("netdev", {})) != set(NETDEVS):
        raise RuntimeError(f"{node}: Ethernet device set is incomplete")

    errors = []
    bytes_by_hca = {}
    for device in RDMAS:
        counters = diff["rdma"][device]
        for name, value in counters.items():
            if value < 0:
                errors.append(f"{device}/{name} reset ({value})")
        rx_bytes = counters["rx_bytes"]
        tx_bytes = counters["tx_bytes"]
        bytes_by_hca[device] = {"rx_bytes": rx_bytes, "tx_bytes": tx_bytes}
        if rx_bytes < min_hca_bytes or tx_bytes < min_hca_bytes:
            errors.append(
                f"{device} carried rx={rx_bytes} tx={tx_bytes}; "
                f"minimum is {min_hca_bytes} each direction"
            )
        for name in (*RDMA_ERRORS, *RDMA_HW_ERRORS):
            if counters[name] != 0:
                errors.append(f"{device}/{name} increased by {counters[name]}")

    for device, counters in diff["netdev"].items():
        for name, value in counters.items():
            if value < 0:
                errors.append(f"{device}/{name} reset ({value})")
        error_names = ["rx_errors", "tx_errors"]
        if device != "enP7s7":
            error_names.extend(("rx_dropped", "tx_dropped"))
        for name in error_names:
            if counters[name] != 0:
                errors.append(f"{device}/{name} increased by {counters[name]}")
    if errors:
        raise RuntimeError(f"{node} counter proof failed: {'; '.join(errors)}")
    return bytes_by_hca


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-runtime", type=int, required=True)
    parser.add_argument("--min-hca-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--log", action="append", type=named_path, required=True)
    parser.add_argument("--diff", action="append", type=named_path, required=True)
    args = parser.parse_args()

    logs = dict(args.log)
    diffs = dict(args.diff)
    expected_nodes = {"cerberus1", "cerberus2", "cerberus3"}
    if set(logs) != expected_nodes or set(diffs) != expected_nodes:
        raise RuntimeError("exactly one log and counter diff per cerberus node is required")
    if args.expected_runtime <= 0 or args.min_hca_bytes <= 0:
        raise RuntimeError("runtime and minimum byte threshold must be positive")

    summary = {"schema": 1, "nodes": {}}
    for node in sorted(expected_nodes):
        result, transport_log = load_result(logs[node])
        diff = json.loads(diffs[node].read_text())
        summary["nodes"][node] = {
            "runtime": validate_result(
                node, result, transport_log, args.expected_runtime
            ),
            "rdma_bytes": validate_diff(node, diff, args.min_hca_bytes),
        }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
