#!/usr/bin/env python3
"""Exercise the exact PyTorch/NCCL stack used by vLLM across both Sparks."""

from __future__ import annotations

import os
import time
import ctypes

import torch
import torch.distributed as dist


def runtime_nccl_version() -> int | None:
    """Return the version from the NCCL object loaded into this process."""
    try:
        library = ctypes.CDLL(None)
        value = ctypes.c_int()
        if library.ncclGetVersion(ctypes.byref(value)) == 0:
            return value.value
    except (AttributeError, OSError):
        pass
    return None


def main() -> None:
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    # 512 MiB per rank; enough traffic for unambiguous NIC byte counters.
    tensor = torch.ones(128 * 1024 * 1024, dtype=torch.float32, device="cuda")
    for _ in range(2):
        dist.all_reduce(tensor)
    torch.cuda.synchronize()
    dist.barrier()
    started = time.perf_counter()
    for _ in range(20):
        dist.all_reduce(tensor)
    torch.cuda.synchronize()
    dist.barrier()
    elapsed = time.perf_counter() - started
    if rank == 0:
        gib = tensor.numel() * tensor.element_size() * 20 / 2**30
        print(
            f"torch={torch.__version__} "
            f"nccl_build={torch.cuda.nccl.version()} "
            f"nccl_runtime={runtime_nccl_version()} "
            f"payload={gib:.1f} GiB elapsed={elapsed:.3f}s "
            f"one_way_payload={gib / elapsed:.2f} GiB/s "
            f"socket_if={os.getenv('NCCL_SOCKET_IFNAME')} "
            f"hcas={os.getenv('NCCL_IB_HCA')}",
            flush=True,
        )
    dist.destroy_process_group()


if __name__ == "__main__":
    main()
