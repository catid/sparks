# DeepSeek V4 Flash NVFP4 + DFlash evaluation

These tools are intentionally separate from the synthetic fixed-token harness.
They measure realistic OpenClaw-style coding-agent turns through the streaming
OpenAI chat API and retain the full request and response for qualitative review.

## Local vLLM/checkpoint compatibility fixes

The downloaded Red Hat DFlash checkpoint ships `fc.weight` with shape
`[4096, 81920]`, but its stronger 500K checkpoint leaves
`target_hidden_size=null`. On both Sparks the local checkpoint metadata is set
to `16384`: five selected target layers × four MHC streams × hidden size 4096
matches the 81,920 projection input exactly.

The installed DeepSeek-V4 target adapter also has a narrowly gated DFlash fix:
its selected auxiliary states preserve all four MHC streams with
`aux_recon.flatten(1)`. Non-DFlash consumers retain the upstream
`aux_recon.mean(dim=1)` behavior. `test_vllm_dsv4_dflash_kv.py` verifies this
shape contract along with the local hybrid-KV grouping fix on both nodes.

The separate [official DSpark checkpoint compatibility audit](DSPARK_OVERLAY.md)
documents why its native-MXFP4 draft shards can be merged structurally but
cannot yet be served safely on top of the converted ModelOpt NVFP4 target.

## Calibrate prompts without inference

The four prompt variants are rendered with NVIDIA's checked-in
`encoding/encoding_dsv4.py` and counted with the checkpoint's local
`tokenizer.json`. No weights are loaded:

```bash
/home/catid/venvs/vllm025/bin/python \
  deepseek-v4/calibrate_prompts.py
```

All four currently render to exactly 1,024 input tokens, including the system
message, terminal schema, Think-Max prefix, and assistant-generation prefix.
The benchmark repeats neutral, task-relevant evidence lines to reach the target;
it does not truncate the task or agent safety instructions.

## Streaming concurrency matrix

For a single two-node TP=2 API:

```bash
/home/catid/venvs/vllm025/bin/python \
  deepseek-v4/benchmark_openclaw.py \
  --endpoints http://127.0.0.1:8000 \
  --metrics-endpoints http://127.0.0.1:8000 \
  --label nvfp4-dflash-tp2 \
  --prompt-variant python_atomic_cache \
  --output results/deepseek-v4-nvfp4-dflash-tp2.json
```

If requests go through the replica router, benchmark that one API endpoint but
scrape both direct vLLM backends:

```bash
/home/catid/venvs/vllm025/bin/python \
  deepseek-v4/benchmark_openclaw.py \
  --endpoints http://127.0.0.1:8080 \
  --metrics-endpoints \
    http://192.168.100.10:8000,http://192.168.100.11:8000 \
  --label nvfp4-dflash-router \
  --output results/deepseek-v4-nvfp4-dflash-router.json
```

Defaults are concurrency 1, 2, 4, 8, 16, and 32, one simultaneous wave at each
level, 1,024 calibrated input tokens, `max_tokens=1024`, streaming usage,
`thinking=true`, and `reasoning_effort=max`. Generation is allowed to stop
naturally or issue a terminal tool call; this is deliberate and makes the rate
representative of agent traffic rather than forced synthetic decoding.
Use `--prompt-variant python_atomic_cache` for the benchmark matrix so every
concurrency level receives the same coding workload. Without that flag, use a
multiple of four waves to balance the four prompt variants.

Artifacts are:

- the requested JSON path, containing configuration, exact calibrated prompts,
  per-batch throughput/latency summaries, finish-reason counts, and DFlash
  counter deltas, including how many requests actually reached the output cap;
- a sibling `*.requests.jsonl`, with every exact request payload/header, exact
  SSE `data:` payload and receive time, reconstructed reasoning/content/tool
  calls, complete usage, TTFT, TTFB, E2E latency, and any error (an
  Authorization value is deliberately redacted);
- a sibling `*.metrics/` directory with raw Prometheus snapshots from before
  and after every concurrency wave.

DFlash summaries include draft steps, draft tokens, accepted tokens, mean draft
length, mean accepted length, and accepted-token rate when all relevant
counters exist. A negative counter delta is marked as a reset and derived rates
are suppressed.

Use `--waves 3` for a less noisy run. Do not combine results from natural-stop
agent prompts with the forced 1,024-output-token synthetic baseline: they answer
different questions.

## Long qualitative agent run

The sandboxed agent evaluator now has a DeepSeek-V4 Think-Max profile:

```bash
python3 agent_eval/run_agent_eval.py \
  --endpoint http://127.0.0.1:8000 \
  --profile deepseek-v4-think-max \
  --tasks ledger_bugfix incident_analysis
```

That profile selects `thinking=true`, `reasoning_effort=max`, temperature/top-p
1.0, a 65,536-token per-turn output allowance, a 30-turn limit, and a two-hour
request timeout. It expects a server context window of at least 393,216 tokens
and rejects a smaller value when `/v1/models` reports one. The client cannot
increase server context: launch vLLM with a suitable `--max-model-len` first.
Every setting remains individually overridable.

A turn that ends with `finish_reason=length` is recorded as
`output_truncated`, and a tool-free response must contain a non-empty final
answer before the task can pass. Long reasoning that merely exhausts the output
allowance is therefore retained for inspection but is not scored as successful
agent completion.
