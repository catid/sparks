#!/usr/bin/env python3
"""Print or compare RDMA and optional management-interface byte counters."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def snapshot(management_interface: str | None = None) -> dict:
    result: dict[str, dict] = {"rdma": {}, "management": {}}
    for device in Path("/sys/class/infiniband").iterdir():
        base = device / "ports/1/counters"
        result["rdma"][device.name] = {
            "rx_bytes": 4 * int((base / "port_rcv_data").read_text()),
            "tx_bytes": 4 * int((base / "port_xmit_data").read_text()),
        }
    if management_interface:
        base = Path("/sys/class/net") / management_interface / "statistics"
        if not base.is_dir():
            raise SystemExit(
                f"management interface is absent: {management_interface}"
            )
        result["management"][management_interface] = {
            "rx_bytes": int((base / "rx_bytes").read_text()),
            "tx_bytes": int((base / "tx_bytes").read_text()),
        }
    return result


def subtract(after: dict, before: dict) -> dict:
    return {
        group: {
            name: {
                metric: after[group][name][metric] - before[group][name][metric]
                for metric in after[group][name]
            }
            for name in after[group]
        }
        for group in after
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path)
    parser.add_argument("--after", type=Path)
    parser.add_argument("--save", type=Path)
    parser.add_argument(
        "--management-interface",
        default=os.environ.get("SPARK_MANAGEMENT_INTERFACE"),
        help=(
            "Optional management netdev; omit it to record only portable "
            "RDMA counters."
        ),
    )
    args = parser.parse_args()

    if args.before and args.after:
        result = subtract(
            json.loads(args.after.read_text()), json.loads(args.before.read_text())
        )
    else:
        result = snapshot(args.management_interface)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
