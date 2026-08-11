#!/usr/bin/env python3
"""Run a three-rank NCCL all-reduce and report the libraries actually loaded."""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import time

import torch
import torch.distributed as dist


def required_int(name: str, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.isdecimal():
        raise RuntimeError(f"{name} must be an unsigned integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be in [{minimum}, {maximum}]")
    return value


def nccl_runtime_version() -> int | None:
    """Ask the NCCL object mapped into this process for its runtime version."""
    for library_name in (None, "libnccl.so.2"):
        try:
            library = ctypes.CDLL(library_name)
            get_version = library.ncclGetVersion
            get_version.argtypes = [ctypes.POINTER(ctypes.c_int)]
            get_version.restype = ctypes.c_int
            result = ctypes.c_int()
            if get_version(ctypes.byref(result)) == 0:
                return result.value
        except (AttributeError, OSError):
            continue
    return None


def loaded_nccl_paths() -> list[str]:
    """Return mapped NCCL paths, proving which container library was used."""
    paths: set[str] = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        fields = line.split()
        if fields and "libnccl.so" in fields[-1] and fields[-1].startswith("/"):
            paths.add(fields[-1])
    return sorted(paths)


def main() -> None:
    rank = required_int("RANK", 0, 2)
    world_size = required_int("WORLD_SIZE", 3, 3)
    tensor_mib = required_int("RING_NCCL_TENSOR_MIB", 16, 2048)
    warmups = required_int("RING_NCCL_WARMUPS", 1, 20)
    iterations = required_int("RING_NCCL_ITERATIONS", 1, 100)
    node = os.environ.get("RING_NCCL_NODE", "")
    if node != f"cerebrus{rank + 1}":
        raise RuntimeError(f"rank {rank} has invalid node identity {node!r}")

    torch.cuda.set_device(0)
    dist.init_process_group(
        backend="nccl",
        init_method="env://",
        rank=rank,
        world_size=world_size,
        timeout=dt.timedelta(minutes=12),
    )

    runtime_version = nccl_runtime_version()
    library_paths = loaded_nccl_paths()
    element_count = tensor_mib * 1024 * 1024 // torch.float32.itemsize
    tensor = torch.empty(element_count, dtype=torch.float32, device="cuda")
    expected = world_size * (world_size + 1) / 2

    for _ in range(warmups):
        tensor.fill_(rank + 1)
        dist.all_reduce(tensor)
    torch.cuda.synchronize()
    dist.barrier()

    started = time.perf_counter()
    for _ in range(iterations):
        tensor.fill_(rank + 1)
        dist.all_reduce(tensor)
    torch.cuda.synchronize()
    dist.barrier()
    elapsed = time.perf_counter() - started

    observed = float(tensor[0].item())
    if observed != expected:
        raise RuntimeError(f"all-reduce result={observed}, expected={expected}")

    build_version = torch.cuda.nccl.version()
    result = {
        "schema": 1,
        "node": node,
        "rank": rank,
        "world_size": world_size,
        "torch_version": torch.__version__,
        "nccl_build_version": (
            list(build_version) if isinstance(build_version, tuple) else build_version
        ),
        "nccl_runtime_version": runtime_version,
        "loaded_nccl_paths": library_paths,
        "tensor_mib": tensor_mib,
        "warmups": warmups,
        "iterations": iterations,
        "elapsed_seconds": elapsed,
        "payload_gib": tensor.numel() * tensor.element_size() * iterations / 2**30,
        "payload_gib_per_second": (
            tensor.numel() * tensor.element_size() * iterations / 2**30 / elapsed
        ),
        "observed_value": observed,
        "expected_value": expected,
        "nccl_ib_hca": os.environ.get("NCCL_IB_HCA"),
        "nccl_socket_ifname": os.environ.get("NCCL_SOCKET_IFNAME"),
        "nccl_subnet_aware_routing": os.environ.get(
            "NCCL_IB_SUBNET_AWARE_ROUTING"
        ),
        "nccl_net_plugin": os.environ.get("NCCL_NET_PLUGIN"),
        "nccl_cross_nic": os.environ.get("NCCL_CROSS_NIC"),
    }
    print(f"RING_NCCL_RESULT={json.dumps(result, sort_keys=True)}", flush=True)
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
