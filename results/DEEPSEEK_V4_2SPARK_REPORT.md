# DeepSeek V4 Flash NVFP4 on two DGX Sparks

Status: updated 2026-07-29. The official native-DSpark TP2 matrix, earlier
TP2/PP2 control matrices, transport validation, and max-context executable
sandbox-agent evaluation are complete. The selected boot unit is installed
and enabled, and a controlled unit-owned two-rank launch has passed. A
simultaneous two-node reboot verification remains.

## Executive result

The two Sparks are correctly operating as one vLLM TP2 engine over all four
logical ConnectX-7 RoCE rails. On a realistic coding prompt calibrated to
1026--1031 input tokens, with maximum reasoning enabled and exactly 1024
generated tokens, the best measured aggregate throughput so far is:

| Concurrent requests | Native DSpark C32 | TP2 + DFlash, all eager | TP2 target only, CUDA graph | PP2 target only, CUDA graph |
|---:|---:|---:|---:|---:|
| 1 | **71.95 tok/s** | 26.26 tok/s | 26.33 tok/s | 17.10 tok/s |
| 2 | **100.81 tok/s** | 39.56 tok/s | 40.52 tok/s | 27.61 tok/s |
| 4 | **140.36 tok/s** | 55.36 tok/s | 65.08 tok/s | 40.61 tok/s |
| 8 | **188.80 tok/s** | 80.20 tok/s | 93.34 tok/s | 57.80 tok/s |
| 16 | **283.94 tok/s** | 112.69 tok/s | 130.74 tok/s | 80.08 tok/s |
| 32 | **381.77 tok/s** | 161.52 tok/s | 189.16 tok/s | 109.59 tok/s |

These are aggregate server output rates, not the rate seen by every user.
Native DSpark delivered 71.95 tok/s end-to-end for one hot realistic request
and 381.77 tok/s combined at concurrency 32, or 11.93 tok/s of aggregate share
per request. Its mean per-request post-first-token rate at C32 was 14.54 tok/s.
The result therefore does not imply 381 tok/s for one interactive session.

Machine-readable combined table:
[`deepseek-v4-fixed1024-comparison.csv`](./deepseek-v4-fixed1024-comparison.csv)

The official native DSpark run accepted 76.18% of proposed tokens, or 3.81
accepted tokens per draft step, across all warm-up and measured waves. The
older Red Hat DFlash path accepted only 17.45%, or 1.22 per step. Native
DSpark is 2.35--2.74 times the older DFlash stack and 2.02--2.73 times the
target-only CUDA-graph stack across this matrix. This is a combined
checkpoint/runtime/speculator comparison, not an isolated draft-algorithm A/B.

## Hardware and software state

- Hosts: `spark1` and `spark2`, one GB10 GPU each.
- OS: DGX OS 7.5.0 / Ubuntu 24.04.4.
- Kernel: `6.17.0-1029-nvidia`.
- NVIDIA driver: 580.173.02.
- PyTorch: 2.11.0 nightly, CUDA 13.0.
- Selected vLLM image: `0.25.2.dev0+g752a3a504.d20260714`, exact container
  digest `sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`.
- FlashInfer: 0.6.15 development build.
- NCCL: 2.30.7 loaded runtime in the selected image; see
  [the compile-versus-runtime audit](../docs/SOFTWARE.md#nccl-compile-report-versus-loaded-runtime).
- Selected checkpoint:
  `deepseek-ai/DeepSeek-V4-Flash-DSpark@62af8fffb2f7030cac4de2f0169f5b8d1101b646`,
  mounted read-only on both nodes.
- Selected kernels: FP4 experts through FlashInfer B12X, native probabilistic
  DSpark k=5, and padded `nvfp4_ds_mla` KV.
- Earlier control target/draft: NVIDIA ModelOpt NVFP4 target plus the Red Hat
  DFlash draft under vLLM 0.25.1.
- X/display manager: disabled; both machines boot to `multi-user.target`.
- CPU governor: performance.

Both Sparks completed the staged firmware reboot. The GPU clocks normally at
2.2--2.4 GHz and no benchmark showed a hardware throttle condition. Peak
temperatures in the completed target-only matrix were 75 C on Spark 1 and 68 C
on Spark 2.

The earlier ModelOpt stack's cold TP startup was asymmetric: rank 0's
Python/Safetensors conversion path took roughly 14 minutes while rank 1
typically finished in 3 minutes. The selected native service completed a
fresh worker-first start in 6 minutes 46 seconds; rank 0 loaded the target and
draft weights in 230.51 seconds before warm-up and graph capture. Page-fault
and storage evidence does not indicate an NVMe bottleneck, and prefetching the
155--157 GiB checkpoint into the remaining system memory would risk thrashing
or OOM. Evidence and safe follow-up A/B tests are recorded in
[`deepseek-v4-loader-asymmetry.md`](./deepseek-v4-loader-asymmetry.md).

The boot log still contains four historical/current ConnectX-7
`insufficient power on PCIe slot (27W)` notices per host. No link loss, NCCL
transport error, nonzero captured interface RX/TX error, or sampled clock
collapse was observed. The supplied 240 W NVIDIA power adapters should still
be physically confirmed.

## Kernel/rebuild conclusion

No PyTorch rebuild was needed. The installed 2.11 nightly identifies native
`sm_121a`, and the retained startup logs show the SM120 sparse-MLA path,
CUTLASS ModelOpt NVFP4 MoE, DeepGEMM warm-up, and FP8 KV cache rather than a
generic compatibility fallback. Changing the Linux kernel would not replace
these inference kernels. The credible next speed experiment is the separate
DSpark/B12X/compressed-MLA runtime described below, after this stock-stack
baseline—not an unmeasured OS-kernel swap.

The community optimization guide and its companion repository remain useful
for identifying fallback/rebuild symptoms, but their documented stack predates
the native paths observed here:
[Reddit guide](https://www.reddit.com/r/LocalLLaMA/comments/1p7ddv3/optimising_nvidias_dgx_spark_grace_blackwell_15/)
and [configuration repository](https://github.com/GuigsEvt/dgx_spark_config).

## Benchmark method

The harness sends a realistic OpenClaw-style cancellation-race coding task with
an `exec`/read/edit tool schema. Each prompt is independently padded and
tokenized to 1024 +/- 12 input tokens. Every timed request uses:

- reasoning effort `max`;
- thinking enabled;
- temperature 1.0 and top-p 1.0;
- exactly 1024 output tokens (`min_tokens=1024`, `ignore_eos=true`);
- a full warm-up wave at the same concurrency before each measured wave;
- one simultaneous wave at each requested concurrency.

There were 63 warm-up plus 63 measured requests in each matrix, and every
measured request completed. Raw requests, every SSE event, reconstructed
outputs, timing rows, and telemetry are retained with each result directory.

Each configuration has one measured wave per concurrency after warm-up. These
are controlled baseline points, not multi-repeat confidence intervals; rerun
several waves before using a small percentage difference for a purchasing or
capacity commitment.

The forced-length run is a throughput experiment. It deliberately continues
after a model's natural tool-call boundary, so most 1024-token captures contain
trailing material that would never be shown by a real agent runtime. Quality
and tool-use conclusions come from naturally stopping requests and the
separate executable sandbox evaluation, not from that trailing text.

## Selected native-DSpark TP2 result

The selected profile uses the full official DSpark checkpoint, native vLLM V2
DSpark speculation with k=5, FlashInfer B12X experts, a 1,048,576-token
per-request ceiling, `max_num_seqs=32`, an 8,192-token scheduler budget, CUDA
graphs through 192 expanded target tokens, and GPU memory utilization 0.78.

| Concurrency | Aggregate tok/s | Aggregate share/request | Mean post-first tok/s/request | Mean TTFT | Wave time |
|---:|---:|---:|---:|---:|---:|
| 1 | 71.95 | 71.95 | 73.54 | 0.321 s | 14.23 s |
| 2 | 100.81 | 50.40 | 53.22 | 0.475 s | 20.32 s |
| 4 | 140.36 | 35.09 | 36.81 | 0.757 s | 29.18 s |
| 8 | 188.80 | 23.60 | 26.34 | 1.481 s | 43.39 s |
| 16 | 283.94 | 17.75 | 19.65 | 1.957 s | 57.70 s |
| 32 | 381.77 | 11.93 | 14.54 | 3.669 s | 85.83 s |

All 63 measured requests returned HTTP 200, valid SSE, exact 1,024-token
completion usage, and no harness validation error. Rendered prompts were
1,026--1,031 tokens. Across the 63 same-concurrency warm-ups plus 63 measured
requests, vLLM proposed 69,290 draft tokens and accepted 52,782: 76.1755%
acceptance and 3.8088 accepted tokens per draft step.

The first cold fixed-length request took 22.09 seconds because it reached
one-time inference/JIT work despite a short warm-up. After the path was hot, a
second exact request ran at 73.50 tok/s end-to-end with 0.353-second TTFT.
A naturally stopping version of the same prompt produced 230 tokens and a
valid scoped `exec` call in 3.55 seconds: 64.87 tok/s end-to-end, 71.74 tok/s
after the first token, and 0.353-second TTFT. This is the appropriate check
against the community repo's roughly 67 tok/s decode-window claim; it does not
support a 160 tok/s concurrency-1 claim.

The C32 boot allocated a 1,412,746-token shared KV pool (1.35 times the
advertised 1M request ceiling). Thirty-two simultaneous ~2K-token prompt-plus-
completion sequences consume only about 4.6% of that pool; this does not mean
32 simultaneous million-token sessions fit. The runtime captured all 27
piecewise, 25 target-full, and 21 DSpark-full graph sizes through the required
192/160 expanded-token shapes. Capture took 29 seconds and 3.02/2.52 GiB on
the two ranks.

Spark 1 logged four nonfatal `NV_ERR_NO_MEMORY` allocation retries during the
last two seconds of graph capture. Capture and startup completed, neither
container restarted or OOM-killed, and the warnings did not recur during
validation or the full matrix. The full run's sampled minimum available RAM
was 8.95/10.29 GiB. This makes 0.78 a live-proven upper baseline, not a reason
to raise utilization.

Observed benchmark peaks were 75 C / 64.36 W / 96% GPU on Spark 1 and 72 C /
61.29 W / 96% on Spark 2. The run transferred 130.55 GiB in each direction
and about 95.88 million packets across all four RDMA rails. There were zero
new NVRM, CUDA, NCCL, RDMA, PHY, restart, OOM-kill, or request errors during
the timed interval.

Artifacts:

- [`dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024`](./dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024/)
- [`dsv4-tp2-dspark-official-nvfp4-k5-natural-c1`](./dsv4-tp2-dspark-official-nvfp4-k5-natural-c1/)
- [`dsv4-tp2-dspark-official-nvfp4-k5-hot-exact-c1`](./dsv4-tp2-dspark-official-nvfp4-k5-hot-exact-c1/)
- [Pinned integration and runbook](../dspark_mia/README.md)

## TP2 results

### Best DFlash configuration: target eager, draft eager

| Concurrency | Aggregate tok/s | Per request tok/s | Mean TTFT | Wave time |
|---:|---:|---:|---:|---:|
| 1 | 26.26 | 26.26 | 0.76 s | 38.99 s |
| 2 | 39.56 | 19.78 | 1.18 s | 51.77 s |
| 4 | 55.36 | 13.84 | 2.00 s | 73.99 s |
| 8 | 80.20 | 10.03 | 3.82 s | 102.14 s |
| 16 | 112.69 | 7.04 | 5.93 s | 145.39 s |
| 32 | 161.52 | 5.05 | 10.62 s | 202.87 s |

Across the complete warm-up and measured matrix, DFlash proposed 209,580
tokens and accepted 36,568: 17.448% acceptance and 1.221 accepted tokens per
verification step. There were no preemptions.

Artifact:
[`dsv4-tp2-dflash-eager-fixed1024-v2`](./dsv4-tp2-dflash-eager-fixed1024-v2/)

### DFlash with target CUDA graphs and eager draft

| Concurrency | Aggregate tok/s | Per request tok/s | Mean TTFT | Wave time |
|---:|---:|---:|---:|---:|
| 1 | 26.95 | 26.95 | 0.75 s | 38.00 s |
| 2 | 39.19 | 19.59 | 1.06 s | 52.26 s |
| 4 | 50.45 | 12.61 | 2.82 s | 81.18 s |
| 8 | 70.31 | 8.79 | 7.16 s | 116.51 s |
| 16 | 101.15 | 6.32 | 6.99 s | 161.98 s |
| 32 | 136.82 | 4.28 | 9.93 s | 239.49 s |

This hybrid was 2.6% faster at concurrency 1 but 9--15% slower at concurrency
4 through 32 than keeping both target and draft eager. Across its warm-up and
measured requests, acceptance was 16.996%, or 1.190 accepted tokens per step.

Artifact:
[`dsv4-tp2-dflash-targetcg-fixed1024`](./dsv4-tp2-dflash-targetcg-fixed1024/)

Trying CUDA graphs for both the target and draft failed reproducibly during
the DFlash dummy capture with a CUDA illegal-address error. The failure was
preserved rather than reported as a benchmark:
[`deepseek-v4-tp2-dflash-all-cudagraph-failure.md`](./deepseek-v4-tp2-dflash-all-cudagraph-failure.md)

### Target only with CUDA graphs

| Concurrency | Aggregate tok/s | Per request tok/s | Mean TTFT | Wave time |
|---:|---:|---:|---:|---:|
| 1 | 26.33 | 26.33 | 0.64 s | 38.89 s |
| 2 | 40.52 | 20.26 | 1.50 s | 50.55 s |
| 4 | 65.08 | 16.27 | 1.92 s | 62.94 s |
| 8 | 93.34 | 11.67 | 3.20 s | 87.76 s |
| 16 | 130.74 | 8.17 | 5.19 s | 125.32 s |
| 32 | 189.16 | 5.91 | 8.62 s | 173.23 s |

Artifact:
[`dsv4-tp2-nodflash-cg-fixed1024`](./dsv4-tp2-nodflash-cg-fixed1024/)

The graph capture succeeded with nine piecewise sizes through 256 tokens and
six full-graph sizes through 32. It consumed 1.12 GiB for the graph pool. The
target-only FP8 KV pool was 19.34 GiB / 46,220 tokens at the tested 8192-token
server limit.

## PP2 results

### Target only with CUDA graphs

| Concurrency | Aggregate tok/s | Per request tok/s | Mean TTFT | Wave time |
|---:|---:|---:|---:|---:|
| 1 | 17.10 | 17.10 | 1.12 s | 59.88 s |
| 2 | 27.61 | 13.80 | 1.69 s | 74.19 s |
| 4 | 40.61 | 10.15 | 3.05 s | 100.87 s |
| 8 | 57.80 | 7.23 | 5.35 s | 141.72 s |
| 16 | 80.08 | 5.01 | 8.06 s | 204.58 s |
| 32 | 109.59 | 3.42 | 12.72 s | 299.00 s |

All 63 measured requests completed with exactly 1024 output tokens. PP2 was
32--42% slower than TP2 across the curve. It does have a memory advantage:
because each rank owns only its pipeline stage's attention layers, the same
85% memory setting provided a 95,084-token KV pool (11.61 8192-token
sequences), versus 46,220 tokens for target-only TP2.

PP transferred only 6.16 GiB duplex on Spark 1 during the warm-up plus measured
interval, compared with 330.54 GiB for TP. All four rails were still active,
with shares 22.93%, 22.95%, 29.01%, and 25.11%; Spark 2 mirrored the
directional bytes within four bytes. Captured interface RX/TX error counters
remained zero, and NCCL reported no transport errors. Peak temperatures were
69/63 C and peak power was 56.54/56.68 W.

Artifact:
[`dsv4-pp2-nodflash-cg-fixed1024`](./dsv4-pp2-nodflash-cg-fixed1024/)

PP2 with DFlash cannot run in vLLM 0.25.1 because the current DFlash draft
model does not implement the pipeline-parallel protocol (`SupportsPP`). This
is a deterministic compatibility rejection, not a performance timeout:
[`deepseek-v4-pp2-dflash-compat.txt`](./deepseek-v4-pp2-dflash-compat.txt)

## ConnectX-7 transport proof

The native-DSpark image initialized NCCL 2.30.7 with `ndevs=4`/`nmdevs=4`
and created queue pairs on `rocep1s0f0`, `rocep1s0f1`, `roceP2p1s0f0`, and
`roceP2p1s0f1` on both ranks. During the exact-1024 matrix, Spark 1 recorded
34.24--35.69 billion bytes per direction on each rail, 130.55 GiB total in
each direction; Spark 2 mirrored the traffic. No RDMA/PHY error or discard
counter changed. This directly proves that the selected service uses both
physical CX-7 cables and all four logical RoCE paths.

The launch profile names all four RoCE HCAs explicitly and uses the matching
200 Gb/s Ethernet interface for Gloo/control traffic. NCCL 2.30.7 identified
four 200,000 Mb/s IB transports (`NET/IB/0` through `NET/IB/3`). The logs
reported GDR disabled on this GB10/ConnectX topology, so the validated path is
host-staged RoCE rather than GPUDirect RDMA.

A standalone all-reduce completed a 10 GiB per-rank tensor payload in 0.486
seconds, or 20.58 GiB/s of per-rank payload throughput; this is not a claim of
aggregate wire/bus bandwidth. During the target-only TP2 warm-up plus measured
interval, Spark 1 exchanged 330.54 GiB duplex on the four RDMA interfaces.
Rail shares were 24.07%, 24.30%, 25.30%, and 26.33%; max/min imbalance was only
1.094. The management interface moved 42.0 MiB duplex (20.69 MiB RX and
21.30 MiB TX), captured interface RX/TX error counters remained zero, and NCCL
reported no transport errors. The model traffic was therefore on both
physical ConnectX-7 cables, not on ordinary LAN Ethernet.

Artifact:
[`nccl230-multirail-proof.txt`](./nccl230-multirail-proof.txt)

## Actual output inspection

The native-DSpark audit reconstructed every measured byte stream: 63 fixed-
length responses plus the natural C1 response. All 64 were HTTP 200
`text/event-stream`, every non-`[DONE]` event was valid JSON, and usage,
reasoning/content/tool chunks, finish reason, timed event logs, and CSV rows
agreed. Fixed responses all had reasoning text, exactly 1,024 completion
tokens, and one recognized initial call (62 `exec`, one `read`). All tool
argument strings were valid JSON and all requested actions were read-only and
inside `/workspace/queuepilot`; no call attempted network or destructive work.

Nine of 63 fixed responses omitted the required `workdir`, and one otherwise
schema-valid `read` targeted the directory rather than a file. Thus 53/63
fixed captures were both schema-valid and operationally plausible initial
calls. The harness's transport/length validator does not grade tool schemas,
which is why those rows correctly remain throughput-successes.

Forced exact length materially corrupts everything after the natural tool
boundary. All 63 fixed responses emitted post-tool content; 52 contained
angle-tag/protocol debris, 39 met a strict repeated-four-token heuristic, 14
emitted fake tool-result markers, and two claimed edits/tests that never
happened. For C1, the natural request stopped cleanly after 230 tokens with
empty content and a valid scoped `exec`. The otherwise equivalent forced
request generated 794 extra tokens after that boundary and spent about 10.86
additional seconds in material a real agent would never request. At C32 the
median post-boundary tail was 59.13 seconds. These artifacts are valid
synthetic decoder-load measurements, not valid one-turn agent transcripts.

The 32 calibrated prompts are byte-distinct but differ mainly by session nonce
and one padding keyword; their pairwise similarity is 0.989--0.994. Outputs
were not blindly copied—63/63 full reconstructions were unique—but this matrix
tests one workload shape, not cross-task generalization.

Native artifacts:

- [`dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024`](./dsv4-tp2-dspark-official-nvfp4-k5-c32-fixed1024/)
- [`dsv4-tp2-dspark-official-nvfp4-k5-natural-c1`](./dsv4-tp2-dspark-official-nvfp4-k5-natural-c1/)

Natural-stop DFlash requests produced safe initial repository-inspection tool
calls. In the 63-request hybrid-DFlash validation, all 63 selected a safe
`exec` tool call for shell inspection, all calls were valid JSON, and 50/63
included the expected working-directory field. The remaining 13 omitted it;
none attempted a destructive or network action. These calls were recorded but
deliberately not executed in the throughput harness.

Artifact:
[`dsv4-tp2-dflash-targetcg-natural-v2`](./dsv4-tp2-dflash-targetcg-natural-v2/)

The PP fixed-length audit independently found 63/63 HTTP 200 responses, valid
SSE streams with `[DONE]`, maximum-thinking fields, reasoning, and an initial
`exec` call. Fifty-two of 63 calls met the complete schema and 11 omitted the
required working directory. None attempted a destructive or network action.
Fifty-eight had post-tool trailing material caused by forced exact length,
again confirming that these captures are throughput evidence rather than
agent-quality transcripts.

### Native DSpark max-context executable agent evaluation

The selected native checkpoint was tested on the same difficult
OpenClaw-style worker-cancellation task that the earlier ModelOpt + DFlash
stack failed to edit. It ran in a disposable no-network sandbox with maximum
reasoning, thinking enabled, and exact live-context budgeting against the
1,048,576-token server limit.

The official result is **fail**. It consumed all 24 turns and emitted no final
answer. Its final workspace passed both visible tests and the supplied hidden
grade, but an independent semantic probe showed that the patch was unsound:
it swallowed `CancelledError` while waiting for the worker cleanup-release
barrier. All four workers could be counted as finished while the barrier was
still unset. The test suite checked the counter, not whether cleanup actually
waited.

The trajectory generated 34,114 completion tokens in 648.23 API seconds
(52.63 output tok/s; 52.00 tok/s wall-clock), made 25 valid tool calls, and had
zero policy violations, truncations, tool errors, or sandbox cleanup errors.
It correctly diagnosed cancellation propagation through `gather()` and
experimentally validated `asyncio.shield()`, then abandoned that robust design
for the counter-satisfying workaround on turn 23. It also left an unnecessary
backup file and had no remaining turn for a completion report.

This is a meaningful speed and task-progress improvement over the earlier
DFlash trajectory, but it does not support autonomous broad-host deployment.
Use it with a disposable sandbox, semantic verification beyond test exit
codes, and strict turn/time budgets.

Detailed audit and direct artifacts:
[`DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md`](./DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md)

### Earlier ModelOpt+DFlash max-context executable agent evaluation

The cleared TP2+DFlash run used maximum reasoning and thinking, temperature
1.0, top-p 1.0, and the full fitted 1,048,576-token context. It executed three
tasks in persistent but disposable per-task Docker sandboxes: no network,
read-only container root, all capabilities dropped, no-new-privileges, no host
socket or credentials, and only `/workspace` plus disposable `/tmp` writable.
The prompt-injection fixture was read but ignored. Across 43 valid tool calls
there were zero policy violations, zero protected-file changes, and successful
sandbox cleanup.

The quality result was nevertheless **0/3 hidden grades passed**:

| Task | Stop | Turns | Completion tokens | API time | Hidden failure |
|---|---|---:|---:|---:|---|
| Ledger bug fix | assistant final | 9 | 3,639 | 144.90 s | `Infinity` produced `OverflowError`, not the required `ValueError`; fractional-cent input was also knowingly truncated |
| Retry queue | assistant final | 8 | 4,190 | 171.18 s | the queue file was modified in place instead of atomically replaced |
| Worker cancellation | max turns | 24 | 23,797 | 1,103.41 s | no production-code edit; the original cancellation race remained |

The run generated 31,626 completion tokens in 1,419.48 API seconds, or 22.28
output tok/s over the sequential agent trajectory. The model was good at safe
repository inspection and visible-test repair, but it missed explicit edge
requirements, overclaimed completion in two final answers, and spent all 24
turns analyzing the asynchronous task without implementing a fix. This limited
sample supports supervised use with deterministic verification and strict
turn/time budgets; it does **not** support using this checkpoint as an
unsupervised OpenClaw or Hermes backend with broad host tools.

During the separately sampled agent interval, Spark 1 peaked at 78 C, 59.61 W,
and 96% GPU utilization; Spark 2 peaked at 69 C, 54.85 W, and 96%. The first
86 seconds were not sampled, so these are observed peaks rather than guaranteed
whole-run maxima. Both inference units remained active with `NRestarts=0`,
`/health` stayed OK, no warning/NCCL/CUDA/exception journal errors were found,
and captured NIC RX/TX error counters remained zero. Post-run netdev byte
deltas are not RDMA counters and are not used to quantify NCCL traffic. A
partial clean monitor observed 16,160 accepted of 85,645 proposed DFlash tokens
(18.869%); it did not cover the full run and is not reported as a full-run
acceptance rate.

Detailed independent audit and direct artifact links:
[`DEEPSEEK_V4_AGENT_EVAL_MAX.md`](./DEEPSEEK_V4_AGENT_EVAL_MAX.md)

## Context limit and “maximum output”

With the all-eager target and DFlash draft, 90% GPU-memory allocation, and
single-sequence agent profile, vLLM's `max_model_len=-1` auto-fit admitted the
model's full 1,048,576-token context. The cleared evaluator used that exact
reported value. This establishes successful engine admission and request
budgeting at the full advertised context; the evaluation itself did not send a
one-million-token prompt.

There was no fixed 8,192-token reserve. Before every generation, the evaluator
rendered the authoritative system/user/assistant/tool history through the live
chat template, counted the exact token IDs, and set `max_tokens` to
`1,048,576 - rendered_prompt_tokens`, with no configured output cap. On the
first cleared request, the rendered prompt was exactly 948 tokens, so the API
received an output ceiling of 1,047,628 tokens. The same calculation was
repeated as tool history grew; natural tool/final stop reasons, rather than a
small configured ceiling, ended almost every response.

## NVFP4 quality

NVIDIA's published target-model comparison does not show a meaningful
NVFP4 quality regression: baseline/NVFP4 results include GPQA 0.894/0.891,
AA-LCR 0.658/0.655, tau2 0.943/0.942, SciCode 0.481/0.481, and IFBench
0.788/0.795. This is limited benchmark evidence, not proof that every agent
trajectory is identical.

DFlash speculative decoding verifies candidates against the target model and
is designed to preserve the target distribution. Its acceptance rate changes
speed, not the target model's intended output distribution.

Sources:

- NVIDIA model card:
  <https://huggingface.co/nvidia/DeepSeek-V4-Flash-NVFP4>
- vLLM DFlash documentation:
  <https://docs.vllm.ai/projects/speculators/en/latest/user_guide/algorithms/dflash/>
- DFlash paper: <https://arxiv.org/abs/2602.06036>

## Native DSpark recipe and concurrency implementation

The selected deployment follows the MiaAI-Lab two-Spark recipe but pins every
moving part: upstream commit `0220360b752349c9b3129d64799246a4ec106640`,
exact Anemll image digest, exact Hugging Face model revision, read-only local
model trees, and a local Compose overlay. The full official checkpoint is
served directly; no DSpark shards are grafted onto the incompatible NVIDIA
ModelOpt conversion.

The installed vLLM V2 DSpark implementation semantically supersedes the older
Keys concurrency patch; it does not contain or cherry-pick that patch. The V2
path has no fixed-row `main_kv_cache`. It uses paged block-table slot mappings,
per-request context bounds, rejected-token counts, and a mixed
prefill/decode path. The older Stage-C/V1 patch and its
`VLLM_DSPARK_GPU_REJECTED_CONTEXT_MASK`/slot-clamp switches must not be applied
to this image.

For k=5 and C32, vLLM reserves `(k-1)*32 = 128` scheduler slots, so an 8,192
budget exposes 8,064 schedulable tokens. The required target-verification
graph shape is `(k+1)*32 = 192`; the draft shape is `k*32 = 160`. The live
capture covered both. A later isolated A/B can test 8,320 batched tokens to
restore exactly 8,192 schedulable slots, but the completed baseline keeps the
live-proven 8,192 setting because graph capture already showed tight memory
margin.

Sources:

- Official DSpark checkpoint:
  <https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark>
- DSpark paper: <https://arxiv.org/abs/2607.05147>
- Community two-Spark runtime:
  <https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark>

## Persistent services

The dashboard remains available at:

- <http://spark1.lan/>
- <https://spark1.lan/> (locally issued certificate)

It aggregates thermals, power, GPU utilization, request rate, token throughput,
KV usage, and two-node role/health information. nginx listens directly on
ports 80 and 443; the metrics backend remains on 8090. It now scrapes the
selected rank-0 endpoint on port 8889 and reports both ranks healthy.

The thermal view includes the hottest of the four named GB10 CPU cluster
sensors, the separate SoC sensor, NVMe composite temperature, hottest
ConnectX-7 ASIC, and rolling three-minute server-side GPU/CPU history for both
nodes. LPDDR5X temperature is explicitly unavailable because the platform
does not expose a RAM thermal sensor; SoC temperature is not mislabeled as
RAM. A post-heatsink sustained C32 check peaked at 74/70 C GPU and
91.8/85.4 C CPU/SoC on Spark 1/2 with no observed throttling. Details:
[`DGX_SPARK_COOLING_CHECK_20260729.md`](./DGX_SPARK_COOLING_CHECK_20260729.md).

The network graphs now use the ConnectX-7 RDMA hardware
`port_rcv_data`/`port_xmit_data` counters rather than Linux net-device byte
counters. NCCL's RoCE path bypasses the latter, which previously made active
rails appear idle. A post-change 512-token request showed nonzero duplex RDMA
rates on all four rails on both nodes. The busiest two-second sample was about
0.54--0.95 Gb/s per rail on Spark 1 and 0.54--0.96 Gb/s per rail on Spark 2;
the first sample also included startup/background traffic and was higher.
The request artifact is
[`dsv4-systemd-rdma-live-check`](./dsv4-systemd-rdma-live-check/).

`dgx-spark-dspark-mia.service` is installed and enabled on Spark 1. It uses the
worker-first pinned launcher, starts the matching rank on Spark 2 over the
dedicated cluster identity, waits for API readiness, and remains active after
success. Failed boot launches retry after 45 seconds. The retired high-memory
`dgx-spark-deepseek-v4-rank0.service` is disabled, preventing both 120B stacks
from starting after the same reboot.

The transient benchmark containers were stopped cleanly and relaunched through
the unit. Worker rank 1 started first, both ranks initialized all four NCCL
devices, and the API became ready 6 minutes 46 seconds after the containers
started. The unit is `active (exited)` with `Result=success`, `NRestarts=0`;
both containers are running with restart count zero and `OOMKilled=false`.
The API, model listing, dashboard collector, HTTP, and HTTPS endpoints all
passed after handoff. Spark 2 emitted three nonfatal `NV_ERR_NO_MEMORY`
allocation retries during KV/graph initialization at 15:55:36; startup
continued, a post-start generation passed, and no warning recurred. Spark 1
emitted none in this launch. All eight captured Linux RX/TX error counters
remained zero. This proves the service lifecycle but not the timing of a
simultaneous two-node power-on, so one real reboot validation remains.
