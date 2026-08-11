#!/usr/bin/env python3
"""Snapshot or subtract exact three-Spark ring RDMA and Ethernet counters."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time


RDMAS = (
    "rocep1s0f0",
    "rocep1s0f1",
    "roceP2p1s0f0",
    "roceP2p1s0f1",
)
NETDEVS = (
    "enp1s0f0np0",
    "enp1s0f1np1",
    "enP2p1s0f0np0",
    "enP2p1s0f1np1",
    "enP7s7",
)
RDMA_DATA = ("port_rcv_data", "port_xmit_data")
RDMA_PACKETS = ("port_rcv_packets", "port_xmit_packets")
RDMA_ERRORS = (
    "port_rcv_errors",
    "port_xmit_discards",
    "symbol_error",
    "link_downed",
    "link_error_recovery",
    "local_link_integrity_errors",
    "excessive_buffer_overrun_errors",
)
RDMA_CONGESTION = ("port_xmit_wait",)
RDMA_HW_ERRORS = (
    "out_of_buffer",
    "out_of_sequence",
    "packet_seq_err",
    "local_ack_timeout_err",
    "req_cqe_error",
    "resp_cqe_error",
    "req_transport_retries_exceeded",
)
NETDEV_COUNTERS = (
    "rx_bytes",
    "tx_bytes",
    "rx_packets",
    "tx_packets",
    "rx_errors",
    "tx_errors",
    "rx_dropped",
    "tx_dropped",
)


def read_counter(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeError(f"cannot read counter {path}: {error}") from error


def snapshot(node: str) -> dict:
    if node not in {"cerebrus1", "cerebrus2", "cerebrus3"}:
        raise RuntimeError(f"invalid node name: {node}")
    result: dict = {
        "schema": 1,
        "node": node,
        "captured_unix_seconds": time.time(),
        "rdma": {},
        "netdev": {},
    }
    for device in RDMAS:
        base = Path("/sys/class/infiniband") / device / "ports/1"
        if not base.is_dir():
            raise RuntimeError(f"missing RDMA device: {device}")
        counters = base / "counters"
        hw_counters = base / "hw_counters"
        values = {
            "rx_bytes": 4 * read_counter(counters / "port_rcv_data"),
            "tx_bytes": 4 * read_counter(counters / "port_xmit_data"),
        }
        values.update({name: read_counter(counters / name) for name in RDMA_PACKETS})
        values.update({name: read_counter(counters / name) for name in RDMA_ERRORS})
        values.update({name: read_counter(counters / name) for name in RDMA_CONGESTION})
        values.update({name: read_counter(hw_counters / name) for name in RDMA_HW_ERRORS})
        result["rdma"][device] = values

    for device in NETDEVS:
        base = Path("/sys/class/net") / device / "statistics"
        if not base.is_dir():
            raise RuntimeError(f"missing Ethernet device: {device}")
        result["netdev"][device] = {
            name: read_counter(base / name) for name in NETDEV_COUNTERS
        }
    return result


def subtract(after: dict, before: dict) -> dict:
    if before.get("schema") != 1 or after.get("schema") != 1:
        raise RuntimeError("unsupported counter snapshot schema")
    if before.get("node") != after.get("node"):
        raise RuntimeError("counter snapshots are from different nodes")
    result = {
        "schema": 1,
        "node": after["node"],
        "elapsed_seconds": (
            after["captured_unix_seconds"] - before["captured_unix_seconds"]
        ),
        "rdma": {},
        "netdev": {},
    }
    for group in ("rdma", "netdev"):
        if set(before[group]) != set(after[group]):
            raise RuntimeError(f"{group} device sets differ between snapshots")
        for device in after[group]:
            if set(before[group][device]) != set(after[group][device]):
                raise RuntimeError(f"{group}/{device} counter sets differ")
            result[group][device] = {
                name: after[group][device][name] - before[group][device][name]
                for name in after[group][device]
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--node", required=True)
    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("--before", type=Path, required=True)
    diff_parser.add_argument("--after", type=Path, required=True)
    args = parser.parse_args()

    if args.action == "snapshot":
        result = snapshot(args.node)
    else:
        result = subtract(
            json.loads(args.after.read_text()),
            json.loads(args.before.read_text()),
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
