#!/usr/bin/env python3
"""Focused in-memory tests for ring NCCL artifact validation."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


BENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BENCH))

from ring_rdma_counters import NETDEVS, RDMA_ERRORS, RDMA_HW_ERRORS, RDMAS  # noqa: E402
from validate_ring_nccl_artifacts import (  # noqa: E402
    CONTAINER_NCCL_PREFIX,
    HCA_SELECTOR,
    validate_diff,
    validate_result,
)


def make_result(node: str) -> dict:
    rank = int(node[-1]) - 1
    return {
        "schema": 1,
        "node": node,
        "rank": rank,
        "world_size": 3,
        "nccl_runtime_version": 23007,
        "loaded_nccl_paths": [
            f"{CONTAINER_NCCL_PREFIX}.2"
        ],
        "observed_value": 6.0,
        "expected_value": 6.0,
        "payload_gib_per_second": 18.5,
        "nccl_ib_hca": HCA_SELECTOR,
        "nccl_socket_ifname": "=enP7s7",
        "nccl_subnet_aware_routing": "1",
        "nccl_net_plugin": "none",
        "nccl_cross_nic": None,
    }


def make_diff(node: str) -> dict:
    errors = {name: 0 for name in (*RDMA_ERRORS, *RDMA_HW_ERRORS)}
    return {
        "schema": 1,
        "node": node,
        "rdma": {
            device: {"rx_bytes": 2_000_000, "tx_bytes": 3_000_000, **errors}
            for device in RDMAS
        },
        "netdev": {
            device: {
                "rx_bytes": 2_000_000,
                "tx_bytes": 3_000_000,
                "rx_errors": 0,
                "tx_errors": 0,
                "rx_dropped": 0,
                "tx_dropped": 0,
            }
            for device in NETDEVS
        },
    }


TRANSPORT = "\n".join(
    ["host NCCL INFO Using network IB"]
    + [f"host NCCL INFO NET/IB: [0] {device}:uverbs" for device in RDMAS]
)


class RingNcclArtifactTests(unittest.TestCase):
    def test_valid_three_node_proof(self) -> None:
        for node in ("cerebrus1", "cerebrus2", "cerebrus3"):
            validate_result(node, make_result(node), TRANSPORT, 23007)
            validate_diff(node, make_diff(node), 1024 * 1024)

    def test_wrong_runtime_fails(self) -> None:
        bad_runtime = make_result("cerebrus1")
        bad_runtime["nccl_runtime_version"] = 22809
        with self.assertRaisesRegex(RuntimeError, "nccl_runtime_version"):
            validate_result("cerebrus1", bad_runtime, TRANSPORT, 23007)

    def test_idle_hca_fails(self) -> None:
        bad_hca = make_diff("cerebrus2")
        bad_hca["rdma"][RDMAS[0]]["rx_bytes"] = 0
        with self.assertRaisesRegex(RuntimeError, "minimum"):
            validate_diff("cerebrus2", bad_hca, 1024 * 1024)

    def test_transport_error_fails(self) -> None:
        bad_error = make_diff("cerebrus3")
        bad_error["rdma"][RDMAS[1]]["packet_seq_err"] = 1
        with self.assertRaisesRegex(RuntimeError, "packet_seq_err"):
            validate_diff("cerebrus3", bad_error, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
