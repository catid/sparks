# DeepSeek V4 Flash three-Spark compatibility result

Status: tested 2026-08-10 against the pinned DSpark/vLLM image and the
`apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8@7d02640c` checkpoint. A
working three-rank model endpoint was not obtained, so there are no valid
three-Spark token-throughput figures.

## Outcome

Three-way tensor parallelism is mathematically invalid for this checkpoint:
its 64 attention heads and 256 routed experts are not divisible by three. The
trial launcher rejects TP3 before touching Docker.

The remaining one-GPU-per-host decomposition is TP1/PP3. To isolate model
compatibility from the then-unproven RoCE ring, the target-only PP3 trial used
NCCL Socket over the management LAN. It successfully:

- formed a three-rank NCCL world;
- assigned the automatic 43-layer partition `14,15,14`; and
- loaded the complete model weights on all three ranks.

This Socket initialization is not evidence that the three-edge IB/RoCE ring
worked; the separate NCCL 2.30.7 IB collective failed as described below.

It then failed during engine initialization, before API readiness, with a
DeepSeek V4 compressed-cache kernel error reporting that
`state_cache.strides[0]` was expected to be divisible by 16. Moving layer
boundaries does not change that cache-layout invariant, so the alternative
`15,15,13` and `16,15,12` profiles were not misreported as performance
experiments. Native DSpark (DFlash in the trial selector) also lacks the
pipeline-parallel protocol in this runtime and is rejected independently.
Pipeline-parallel support also remained unchecked in the upstream
[vLLM DeepSeek V4 roadmap](https://github.com/vllm-project/vllm/issues/40902)
at the time of this test.

## Requested fixed-length matrix

The planned workload was a realistic coding-agent prompt calibrated to about
1,024 input tokens, maximum thinking, exactly 1,024 generated tokens, and
three measured repeats after warm-up.

| Concurrent requests | TP1/PP3 target-only | TP1/PP3 + DSpark/DFlash |
| ---: | ---: | ---: |
| 1 | unavailable | unsupported |
| 2 | unavailable | unsupported |
| 4 | unavailable | unsupported |
| 8 | unavailable | unsupported |

“Unavailable” means engine initialization failed; it does not mean zero
tokens per second. Publishing zeros would incorrectly turn a compatibility
failure into a throughput measurement.

## Three-node transport finding

The first physical ring used C1-P0↔C3-P0 and C2-P1↔C3-P1. All twelve logical
interfaces passed carrier, 200-Gb/s link speed, MTU 9000, exact peer ping, and
RDMA `ACTIVE/LINK_UP` checks. That was still insufficient for a three-rank
collective. NCCL 2.30.7 timed out when C3 P0 (`192.168.2.2`) attempted a QP to
C2 P0 (`192.168.0.2`), once with NCCL's default cross-NIC policy and again
with `NCCL_CROSS_NIC=1`.

The reproducible target is NVIDIA's all-cross orientation: C1-P1↔C2-P0,
C2-P1↔C3-P0, and C3-P1↔C1-P0. Only C3's two cable ends need to be exchanged;
the explicit `c3-p0-to-c2` Netplan profile then places the same subnets on the
new facing ports. The isolated NCCL verifier must pass with runtime version
23007 and positive, error-free counters on all twelve HCAs before the ring is
called proven.
The addressing and crossed-port layout follow NVIDIA's
[Connect Three Sparks playbook](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-three-sparks).

## Production decision

Keep the serving service on C1 and C2 as TP2/PP1 with native DSpark k=5. It
uses only their direct C1-P1↔C2-P0 edge, and its supervisor intentionally has
no dependency on C3. C3 remains useful for transport validation and separate
workloads. Revisit three-rank DS4F only after a vLLM/DeepSeek V4 runtime adds
working pipeline-parallel compressed-cache support and DSpark PP support, or
after a model geometry compatible with TP3 becomes available.

## Current TP2 direct-edge baseline

After restoring the active C1+C2 service, the fixed workload was run three
times per concurrency against the one physical C1-P1↔C2-P0 cable (two logical
RoCE links). Prompts rendered to 1,027–1,030 tokens; every one of the 45
measured requests and 15 warm-ups returned valid streaming transport and
exactly 1,024 output tokens.

| Concurrency | Aggregate output tok/s, three repeats | Median | Mean | Median TTFT |
| ---: | --- | ---: | ---: | ---: |
| 1 | 45.31, 64.72, 78.17 | 64.72 | 62.73 | 0.394 s |
| 2 | 91.05, 107.46, 116.33 | 107.46 | 104.95 | 0.464 s |
| 4 | 157.66, 131.41, 146.27 | 146.27 | 145.11 | 0.656 s |
| 8 | 222.99, 219.43, 203.41 | 219.43 | 215.28 | 1.145 s |

The first C1 and C2 waves included one-time 7.81-second and 5.23-second TTFTs;
later repeats were hot. Native DSpark accepted 38,460 of 48,100 draft tokens
(79.96%), or 3.998 accepted tokens per draft step.

Counter proof was unambiguous: C1's two P1 HCAs transferred about 47.25 and
47.85 GiB in each direction, and C2's P0 HCAs recorded the exact inverse.
C1 P0, C2 P1, and all four C3 HCAs changed by exactly zero bytes. No captured
RDMA error, congestion, retry, or hardware-error counter increased. Peak GPU
temperatures were 74°C on C1 and 71°C on C2; idle C3 remained at 40°C.

This forced-length matrix is not an agent-quality evaluation. Requiring
`min_tokens=1024` with `ignore_eos` made the model continue after its natural
tool boundary: 44/45 eventual tool calls had the complete schema and all were
read-only, but many responses first hallucinated tool results, emitted DSML
artifacts, irrelevant commands, or garbage. Use natural EOS and executable
sandbox turns to assess OpenClaw behavior; use this table only for comparable
server throughput.

The isolated reproduction launcher and future benchmark contract are retained
under [`dspark_mia3`](../dspark_mia3/README.md) and
[`deepseek_v4_bench`](../deepseek_v4_bench/README.md).
