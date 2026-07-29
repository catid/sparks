# Laguna S 2.1 baseline on two DGX Sparks

Measured July 28, 2026 with vLLM 0.25.1, PyTorch 2.11.0+cu130, and
FlashInfer 0.6.15.dev20260712. Every row is one simultaneous batch with 1,024
input tokens and 1,024 output tokens per request, EOS ignored, temperature 0,
and prefix caching disabled. All requests completed successfully with the exact
token counts.

## Aggregate output throughput

| Concurrency | FP8 PP=2 | NVFP4 2 replicas | NVFP4 / FP8 | NVFP4+DFlash K=15 | DFlash / NVFP4 |
|---:|---:|---:|---:|---:|---:|
| 1 | 16.24 tok/s | 19.46 tok/s | 1.20x | 157.96 tok/s | 8.12x |
| 2 | 27.25 tok/s | 38.79 tok/s | 1.42x | 261.97 tok/s | 6.75x |
| 4 | 37.45 tok/s | 70.95 tok/s | 1.89x | 161.93 tok/s | 2.28x |
| 8 | 53.38 tok/s | 114.13 tok/s | 2.14x | 272.44 tok/s | 2.39x |
| 16 | 73.04 tok/s | 175.10 tok/s | 2.40x | 778.67 tok/s | 4.45x |
| 32 | 103.01 tok/s | 271.03 tok/s | 2.63x | 257.00 tok/s | 0.95x |

FP8 uses TP=1 and PP=2 across the two Sparks because the checkpoint does not fit
with serving overhead on one Spark. NVFP4 uses one TP=1/PP=1 replica on each
Spark and round-robins requests, which is the faster no-tensor-parallel layout.

## Latency

| Concurrency | FP8 TTFT | FP8 batch wall | NVFP4 TTFT | NVFP4 batch wall | DFlash TTFT | DFlash batch wall |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.807 s | 63.05 s | 0.462 s | 52.63 s | 0.581 s | 6.48 s |
| 2 | 1.129 s | 75.16 s | 0.445 s | 52.79 s | 0.566 s | 7.82 s |
| 4 | 1.577 s | 109.36 s | 0.684 s | 57.73 s | 1.360 s | 25.29 s |
| 8 | 2.695 s | 153.46 s | 1.071 s | 71.78 s | 1.383 s | 30.07 s |
| 16 | 8.158 s | 224.32 s | 1.964 s | 93.57 s | 2.516 s | 21.04 s |
| 32 | 15.916 s | 318.11 s | 3.908 s | 120.90 s | 4.562 s | 127.50 s |

## DFlash caveat

The exact-token harness uses distinct synthetic token-ID prompts. DFlash
acceptance varied drastically by continuation: server telemetry ranged from
zero acceptance and mean accepted length 1.0 to roughly 83% acceptance and mean
accepted length 13.4 of 15. This explains the non-monotonic throughput and the
C=32 regression. At C=32 the median request finished in 24.35 seconds, but two
zero-acceptance stragglers raised p95 to 81.39 seconds and batch wall time to
127.50 seconds. DFlash should therefore be re-measured with representative
prose/code prompts before enabling it as a latency-sensitive default.

FP8+DFlash could not be measured without tensor parallelism: FP8 needs PP=2 to
fit, and vLLM 0.25.1 rejects the Laguna DFlash drafter under pipeline
parallelism because it does not implement `SupportsPP`.

## Network verification

The FP8 PP=2 serving matrix used NCCL over RDMA. Spark 1 transmitted 498.0 MB,
432.0 MB, 427.6 MB, and 292.0 MB on the four RoCE functions; Spark 2 recorded
the matching receives. Management Ethernet carried only control traffic.
Earlier paired NCCL/PyTorch testing measured 22.86 GiB/s payload throughput and
showed all four RoCE functions active.

The NVFP4 layout has no model communication: each Spark holds a complete
replica. Requests to Spark 2 use its `192.168.100.11` ConnectX-7 address.

## Raw artifacts

- `fp8-baseline.json` and `fp8-baseline.requests.csv`
- `nvfp4-baseline.json` and `nvfp4-baseline.requests.csv`
- `nvfp4-dflash-k15.json` and `nvfp4-dflash-k15.requests.csv`
- `*-network-delta.json` for per-run network counter deltas
- matching server logs under `../logs/`
