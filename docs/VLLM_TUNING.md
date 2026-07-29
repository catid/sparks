# vLLM and DSpark tuning

The current baseline is the official DeepSeek V4 Flash DSpark NVFP4
checkpoint served by the pinned vLLM image across two DGX Sparks. It uses
native DSpark speculative decoding, tensor parallelism across the two
machines, thinking mode, and all four logical ConnectX-7 RoCE rails.

## C32 throughput baseline

The retained throughput profile is intentionally conservative at startup while still
admitting 32-request waves:

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
same runtime for C1-C8 agent traffic:

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

The deployed speculative path is not the older ModelOpt DFlash side model. It
uses the draft capability packaged with the official
`DeepSeek-V4-Flash-DSpark` NVFP4 checkpoint and vLLM's native
`"method":"dspark"` proposer. The selected k5 configuration was much faster
than the tested DFlash side-drafter profiles and had materially higher draft
acceptance. In the fixed 1,024-in/1,024-out matrix, native DSpark ranged from
71.95 aggregate output tok/s at concurrency 1 to 381.77 at concurrency 32;
the best earlier DFlash profile ranged from 26.26 to 161.52. Native DSpark
accepted 76.18% of proposed draft tokens versus 17.45% for that DFlash run.
This is a checkpoint/runtime/speculator comparison, not an isolated algorithm
A/B. Recorded comparisons and limitations are in
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
--max-num-seqs 32
--max-num-batched-tokens 8192
--max-cudagraph-capture-size 192
--gpu-memory-utilization 0.78
--enable-prefix-caching
--async-scheduling
--enable-chunked-prefill
```

At k5, the pinned runtime reserves `max_num_seqs * (k - 1)` draft slots. For
32 sequences that is 128 slots, leaving 8,064 scheduled tokens from the 8,192
budget. Chunked prefill admits a 32-by-approximately-1,024-token wave in
bounded pieces rather than requiring all prompt activations at once.

The graph ceiling is `max_num_seqs * (k + 1)`, or `32 * 6 = 192`. Static
validation checks this relationship. The tested image captured the complete
target and DSpark graph sets through concurrency 32.

`GPU_MEMORY_UTILIZATION=0.78` is a live-proven ceiling, not spare budget to
consume casually. The baseline captured graphs successfully, but late capture
produced nonfatal allocation retries and the lowest observed free system
memory was under 9 GiB on one node. Raising the fraction risks failing cold
startup even if a hot process appears comfortable.

The C32 cold-start log reported a 1,464,202-token KV pool and 2.59 GiB of
captured graphs. One later `cache_config_info` sample disagreed at 886,775
tokens, so the coordinated cold-start log remains the capacity authority. The
active C8 cold start reported 1,417,464 tokens and 1.27 GiB of graphs. Its
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

The selected model path uses:

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

Throughput is not a quantization-quality result. This repository does not yet
contain a controlled FP8-versus-NVFP4 agent-capability evaluation of the same
checkpoint, prompts, sampling, and grader. The recorded NVFP4 agent trial is
useful evidence about one difficult workflow, but it cannot quantify how much
quality the quantization changes.

## NCCL and the four-rail fabric

Two physical ConnectX-7 cables expose four logical RDMA paths on each Spark.
The serving profile names all four HCAs:

```text
=rocep1s0f0:1:0,roceP2p1s0f0:1:0,rocep1s0f1:1:1,roceP2p1s0f1:1:1
```

The relevant transport policy is:

```text
NCCL_NET=IB
NCCL_IB_DISABLE=0
NCCL_NETDEVS_POLICY=ALL
NCCL_CROSS_NIC=0
NCCL_IB_MERGE_NICS=0
NCCL_SOCKET_IFNAME="=enp1s0f0np0"
TP_SOCKET_IFNAME=enp1s0f0np0
GLOO_SOCKET_IFNAME=enp1s0f0np0
NCCL_DMABUF_ENABLE=1
NCCL_NET_GDR_C2C=1
NCCL_IB_QPS_PER_CONNECTION=1
NCCL_IB_SPLIT_DATA_ON_QPS=0
NCCL_CUMEM_ENABLE=0
NCCL_NVLS_ENABLE=0
```

There is deliberately no scalar `NCCL_IB_GID_INDEX`. The local Compose
override removes an inherited value, the launch wrapper unsets it, and static
validation checks that it did not leak back in.

The 2026 baseline proved the path from NCCL initialization and hardware
counters, not from Linux netdev counters alone:

- NCCL 2.30.7 reported `ndevs=4` and `nmdevs=4`;
- it created queue pairs on all four named RDMA devices on both ranks;
- every rail transferred substantial, near-balanced traffic during the fixed
  1,024-token matrix;
- Spark 2 mirrored the directional traffic; and
- no RDMA/PHY error or discard counter increased.

RoCE traffic bypasses normal Linux netdev byte accounting. Use
`/sys/class/infiniband/*/ports/1/counters/port_{rcv,xmit}_data`, the supplied
counter helper, or the dashboard. One busy Ethernet interface in a generic
network graph does not show that NCCL is using only one rail.

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
Spark 1 supervisor perform a coordinated cold reload, then verify both ranks
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
