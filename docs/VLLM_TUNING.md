# vLLM and DSpark tuning

The active service selects the pinned
`apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8` revision, native DSpark
speculative decoding, TP2 across C1 and C2, thinking mode, and
`nvfp4_ds_mla` KV cache. Production NCCL uses the two logical RoCE links on
the direct C1-P1/C2-P0 edge. The original
`deepseek-ai/DeepSeek-V4-Flash-DSpark` checkpoint and former four-link fabric
remain important historical benchmark references, but they are not the
current model or topology.

## Historical C32 throughput baseline

The retained C32 profile is intentionally conservative at startup while still
admitting 32-request waves. The measurements in this section used the original
official NVFP4 checkpoint on the former four-link C1-C2 fabric; they have not
been repeated as a matched matrix with the active abliterated FP8 checkpoint
on the ring's two-link production edge:

| Setting | Value |
| --- | ---: |
| Tensor parallel size | 2 |
| Pipeline parallel size | 1 |
| Nodes / rank placement | 2 / one rank per Spark |
| Model quantization | native NVFP4 checkpoint |
| KV cache | `nvfp4_ds_mla` |
| Maximum model length | 1,048,576 tokens |
| Maximum sequences | 32 |
| Maximum batched tokens | 8,192 |
| DSpark speculative tokens | 5 |
| Draft sampling | probabilistic |
| Maximum CUDA-graph capture | 192 expanded slots |
| GPU memory utilization | 0.78 |
| Thinking | enabled by default |

The vLLM command is rendered from
[`compose.mia.override.yml`](../dspark_mia/compose.mia.override.yml), while
profile-specific capacities and fabric values come from the selected `.env`
file.

## Low-concurrency OpenClaw profile

The credential-free
[`mia-agent.env.example`](../dspark_mia/mia-agent.env.example) specializes the
same scheduler/runtime family for C1-C8 agent traffic. The following cold-start
and throughput results also came from the original official checkpoint and
former four-link fabric, while the selected scheduler values remain the active
agent configuration:

| Setting | Agent value |
| --- | ---: |
| Maximum model length | 1,048,576 tokens |
| Maximum sequences | 8 |
| Maximum batched tokens | 8,192 |
| DSpark speculative tokens | 5 |
| Maximum CUDA-graph capture | 48 expanded slots |
| GPU memory utilization | 0.78 |

At k5, eight target sequences require a graph ceiling of
`8 * (5 + 1) = 48`, and vLLM reserves `8 * (5 - 1) = 32` draft scheduler
slots. Requests beyond C8 queue. This is appropriate when OpenClaw normally
runs one or two turns and only occasionally fans out to a few subagents. It
avoids capturing graph sizes that an agent host is not expected to use while
preserving long-context capacity, prefix caching, asynchronous scheduling,
and chunked prefill.

The cold C8 launch succeeded at the live-proven 0.78 memory fraction. It
captured 1.27 GiB of graphs in about seven seconds and reported a
1,417,464-token KV pool; the C32 baseline captured 2.59 GiB in about 24
seconds and reported 1,464,202 KV tokens.

Three warmed C8 waves measured 73.60, 101.21, 141.89, and 186.92 aggregate
output tok/s at C1/C2/C4/C8. Against the identical C32 baseline, the changes
were +2.3%, +0.4%, +1.1%, and -1.0%: effectively neutral. C8 is selected for
the smaller graph footprint and workload-appropriate queue ceiling, not a
claimed decode speedup. Full method, TTFT, response inspection, and limitations
are in
[`DEEPSEEK_V4_C8_AGENT_PROFILE.md`](../results/DEEPSEEK_V4_C8_AGENT_PROFILE.md).

Keep the 8,192-token prefill budget unless a controlled long-prefill test
proves a reason to change it. A 4,096-token follow-up could improve decode
fairness while another request prefills, but can also add prefill iterations
and worsen time to first token.

## Why TP2/PP1

Each Spark holds one TP rank. TP2 was selected after measuring TP2 and PP2 on
the same two-node hardware and 1,024-in/1,024-out concurrency matrix. The
target-only PP2 path was 32–42% slower across the tested curve. PP2 reduced
inter-node traffic and increased target-only KV capacity, but those benefits
did not compensate for pipeline bubbles in this workload.

The earlier DFlash side-drafter implementation also did not support the
pipeline-parallel protocol in the tested vLLM runtime. It was therefore not a
viable PP2 speculative configuration.

The deployed speculative path is not the older ModelOpt DFlash side model. The
active abliterated FP8 checkpoint retains the DeepSeek V4 native DSpark draft
capability, and vLLM uses its `"method":"dspark"` proposer at k5. The speed and
acceptance figures below are historical results for the original official
NVFP4 checkpoint on the former four-link fabric, not measurements of the
current abliterated/two-link deployment. In that fixed 1,024-in/1,024-out
matrix, native DSpark ranged from 71.95 aggregate output tok/s at concurrency
1 to 381.77 at concurrency 32; the best earlier DFlash profile ranged from
26.26 to 161.52. Native DSpark accepted 76.18% of proposed draft tokens versus
17.45% for that DFlash run. This is a checkpoint/runtime/speculator comparison,
not an isolated algorithm A/B. Recorded comparisons and limitations are in
[`DEEPSEEK_V4_2SPARK_REPORT.md`](../results/DEEPSEEK_V4_2SPARK_REPORT.md).

For the earlier DFlash stack, keeping target and draft eager was the best
measured high-concurrency configuration. Target CUDA graphs plus an eager
draft gained slightly at concurrency 1 but lost at concurrency 4–32. Capturing
both target and DFlash draft graphs failed reproducibly with a CUDA illegal
address during dummy capture and was recorded as a failure, not a benchmark.
Those DFlash-specific findings should not be generalized to the native DSpark
graph path used here.

## Scheduler and graph sizing

The important scheduler flags are:

```text
--max-model-len 1048576
--max-num-seqs 8
--max-num-batched-tokens 8192
--max-cudagraph-capture-size 48
--gpu-memory-utilization 0.78
--enable-prefix-caching
--async-scheduling
--enable-chunked-prefill
```

Those are the active C8 agent values. The retained throughput profile changes
`max-num-seqs` to 32 and `max-cudagraph-capture-size` to 192. At k5, the
pinned runtime reserves `max_num_seqs * (k - 1)` draft slots: 32 for C8, or
128 for C32. The latter leaves 8,064 scheduled tokens from the 8,192 budget.
Chunked prefill admits a 32-by-approximately-1,024-token throughput wave in
bounded pieces rather than requiring all prompt activations at once.

The graph ceiling is `max_num_seqs * (k + 1)`: `8 * 6 = 48` for the active
agent profile and `32 * 6 = 192` for the throughput profile. Static validation
checks this relationship. The historical official-checkpoint test captured
the complete target and DSpark graph sets through concurrency 32.

`GPU_MEMORY_UTILIZATION=0.78` is a live-proven ceiling, not spare budget to
consume casually. The historical official-checkpoint baseline captured graphs
successfully, but late capture produced nonfatal allocation retries and the
lowest observed free system memory was under 9 GiB on one node. Raising the
fraction risks failing cold startup even if a hot process appears comfortable.

The historical official-checkpoint C32 cold-start log reported a
1,464,202-token KV pool and 2.59 GiB of captured graphs. One later
`cache_config_info` sample disagreed at 886,775 tokens, so the coordinated
cold-start log remains the capacity authority for that run. The corresponding
historical C8 cold start reported 1,417,464 tokens and 1.27 GiB of graphs. Its
later live metric reported 858,469 tokens (`0.8187` maximum concurrency),
leaving only 72,037 tokens above two configured 393,216-token OpenClaw working
contexts. The reason for that cold-log/runtime-metric disagreement is not yet
resolved. Use early compaction and do not treat the two-context arithmetic as
a guarantee that both sessions can fill their configured window
simultaneously.

The one-million-token number is a per-request model ceiling, not a guarantee
that 32 one-million-token conversations fit concurrently. The measured shared
KV pool was about 1.41 million tokens. Thirty-two short coding requests fit;
thirty-two maximum-context sessions do not.

## Model-specific execution

The active model path uses:

```text
--kv-cache-dtype nvfp4_ds_mla
--block-size 256
--moe-backend flashinfer_b12x
--tokenizer-mode deepseek_v4
--tool-call-parser deepseek_v4
--enable-auto-tool-choice
--reasoning-parser deepseek_v4
--default-chat-template-kwargs {"thinking":true}
--enable-flashinfer-autotune
```

The profile also targets GB10/SM121a and enables the tested FlashInfer
sampler/B12X path:

```text
CUTE_DSL_ARCH=sm_121a
TORCH_CUDA_ARCH_LIST=12.1a
FLASHINFER_CUDA_ARCH_LIST=12.1a
VLLM_USE_FLASHINFER_SAMPLER=1
VLLM_USE_B12X_MOE=1
```

Do not transplant these flags to a different checkpoint or generic vLLM
image without revalidation. `nvfp4_ds_mla`, the DeepSeek V4 parsers, DSpark
proposer, and B12X kernels are model/runtime-specific.

Thinking is enabled at the chat-template layer. Agent clients should still
send `reasoning_effort: "max"` when maximum reasoning is desired and should
set a deliberate `max_tokens` budget. That budget must leave room for the
rendered prompt inside the one-million-token context. For quality evaluation,
allow the model to stop naturally; forcing `min_tokens=max_tokens` and
`ignore_eos=true` is only for fixed-length throughput comparisons.

Throughput is not a quantization-quality result. The active repository is
labelled FP8 while the reference checkpoint is native NVFP4, so they also
differ in model release and ablation—not just storage format. This repository
does not yet contain a controlled FP8-versus-NVFP4 agent-capability evaluation
of otherwise identical weights, prompts, sampling, and grader. The recorded
NVFP4 agent trial is useful evidence about one difficult workflow, but it
cannot quantify how much quality the quantization changes.

## NCCL on the production ring edge

The three-node physical ring is documented in [NETWORKING.md](NETWORKING.md).
Production TP2 uses only the direct C1-P1 to C2-P0 edge, whose facing HCA
names differ by rank:

```text
HEAD_NCCL_IB_HCA='=rocep1s0f1:1:0,roceP2p1s0f1:1:0'
WORKER_NCCL_IB_HCA='=rocep1s0f0:1:0,roceP2p1s0f0:1:0'
```

The relevant transport policy is:

```text
NCCL_NET=IB
NCCL_IB_DISABLE=0
NCCL_NETDEVS_POLICY=ALL
NCCL_CROSS_NIC=0
NCCL_IB_MERGE_NICS=0
NCCL_SOCKET_IFNAME="=enP7s7"
TP_SOCKET_IFNAME=enP7s7
GLOO_SOCKET_IFNAME=enP7s7
NCCL_DMABUF_ENABLE=1
NCCL_NET_GDR_C2C=1
NCCL_IB_QPS_PER_CONNECTION=1
NCCL_IB_SPLIT_DATA_ON_QPS=0
NCCL_CUMEM_ENABLE=0
NCCL_NVLS_ENABLE=0
```

There is deliberately no scalar `NCCL_IB_GID_INDEX`. The local Compose
override removes an inherited value, the launch wrapper unsets it, injects the
appropriate HCA expression per rank, and static validation checks both
renderings.

The historical pre-ring 2026 baseline proved its former four-logical-link path
from NCCL initialization and hardware counters, not from Linux netdev counters
alone:

- NCCL 2.30.7 reported `ndevs=4` and `nmdevs=4`;
- it created queue pairs on all four named RDMA devices on both ranks;
- every rail transferred substantial, near-balanced traffic during the fixed
  1,024-token matrix;
- C2 mirrored the directional traffic; and
- no RDMA/PHY error or discard counter increased.

RoCE traffic bypasses normal Linux netdev byte accounting. Use
`/sys/class/infiniband/*/ports/1/counters/port_{rcv,xmit}_data`, the supplied
counter helper, or the dashboard. One busy Ethernet interface in a generic
network graph does not show which RDMA path NCCL selected. After the ring
migration, require positive production deltas on C1 P1 and C2 P0; the other
ring ports may remain idle. Historical four-path throughput is not a current
baseline and must be remeasured.

The service actually maps NCCL 2.30.7 from the container's Python
distribution. `torch.cuda.nccl.version()` returns 2.28.9 because it reports
PyTorch build metadata. To audit a live rank, resolve its exact labelled
container, get its host PID, and inspect the mapped library:

```bash
container="$(
  sudo docker ps -q \
    --filter label=com.docker.compose.project=mia-dspark-agent \
    --filter label=com.docker.compose.service=vllm-dspark
)"
pid="$(sudo docker inspect --format '{{.State.Pid}}' "${container}")"
sudo awk '$0 ~ /libnccl[.]so/ {print $6}' "/proc/${pid}/maps" | sort -u
```

Use the same check on both ranks. A version string from an unused system
library is not evidence about the running process.

## Resource-limit change

The Compose overlay requests `nofile` soft/hard limits of 500,000. This avoids
the low soft limit observed on the original worker container during
high-concurrency, long-lived API use. The change applies only when Compose
recreates the two containers. Do not restart one rank to pick it up; let the
C1 supervisor perform a coordinated cold reload, then verify both ranks
as described in [CONTAINERS.md](CONTAINERS.md#runtime-isolation).

## Change discipline

Make experiments in a new local profile and use different API, rendezvous,
Compose-project, state, and temporary-directory names. Before launch:

```bash
MIA_ENV_FILE=experiment.local.env ./dspark_mia/bin/validate-static.sh
MIA_ENV_FILE=experiment.local.env ./dspark_mia/bin/sync-worker.sh
MIA_ENV_FILE=experiment.local.env ./dspark_mia/bin/preflight.sh
```

The final command requires the production workload to be stopped and ports to
be free. Never let an experiment implicitly stop the serving profile, and
never compare profiles without preserving input/output tokens, warm-up,
concurrency, endpoint topology, and generated responses.
