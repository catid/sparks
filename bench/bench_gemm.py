#!/usr/bin/env python3
"""Synchronized GB10 GEMM benchmark.

The commonly circulated DGX Spark script queues work for 60 seconds and only
synchronizes afterward, so its loop timer does not measure completed GEMMs.
This version uses CUDA events and reports only synchronized device time.
"""

from __future__ import annotations

import argparse
import json
import statistics

import torch


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=8192)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dtype", choices=("float16", "bfloat16"), default="float16")
    args = parser.parse_args()

    dtype = getattr(torch, args.dtype)
    n = args.size
    a = torch.randn((n, n), device="cuda", dtype=dtype)
    b = torch.randn((n, n), device="cuda", dtype=dtype)
    out = torch.empty((n, n), device="cuda", dtype=dtype)

    for _ in range(args.warmup):
        torch.mm(a, b, out=out)
    torch.cuda.synchronize()

    samples = []
    for _ in range(args.repeats):
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(args.iterations):
            torch.mm(a, b, out=out)
        end.record()
        end.synchronize()
        elapsed_s = start.elapsed_time(end) / 1000.0
        samples.append(2.0 * n**3 * args.iterations / elapsed_s / 1e12)

    result = {
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "device": torch.cuda.get_device_name(),
        "capability": list(torch.cuda.get_device_capability()),
        "dtype": args.dtype,
        "size": n,
        "iterations": args.iterations,
        "tflops_samples": samples,
        "tflops_median": statistics.median(samples),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
