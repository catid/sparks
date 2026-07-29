# DeepSeek V4 Flash C8 agent profile

## Decision

Keep the C8 profile for the OpenClaw deployment. At the intended C1-C8 load,
decode performance is effectively tied with the C32 throughput profile, while
the smaller graph set releases about 1.32 GiB and materially shortens cold
capture. Switch back to C32 only when more than eight simultaneously scheduled
requests is a real requirement.

This is scheduler/capture tuning of the same NVFP4 checkpoint, TP2 placement,
native DSpark k5 proposer, vLLM image, KV format, and four-rail NCCL topology.
It is not a quantization or model-quality comparison.

## Fixed 1,024-in/1,024-out matrix

The live C8 generation received three measured waves at each concurrency after
a same-concurrency warm-up. Prompts were realistic coding-agent requests
calibrated to 1,027-1,030 reported input tokens. Requests used thinking mode,
`reasoning_effort=max`, temperature 1, top-p 1, streaming, and an exact
1,024-token output constraint.

| Concurrency | C8 aggregate output tok/s | C32 baseline tok/s | Difference | C8 mean post-first tok/s/request | C8 mean TTFT |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 73.60 | 71.95 | +2.30% | 75.68 | 0.382 s |
| 2 | 101.21 | 100.81 | +0.40% | 52.77 | 0.521 s |
| 4 | 141.89 | 140.36 | +1.09% | 38.05 | 0.737 s |
| 8 | 186.92 | 188.80 | -1.00% | 26.51 | 1.119 s |

The C32 reference has one measured wave per point, so the small differences
above should be read as neutral rather than as a statistically established
speedup. At C8, the C8 profile's mean TTFT was 24.5% lower; at C1 it was
0.061 seconds higher. Median end-to-end latency differed by less than 5% at
every point.

All 45 C8 requests returned HTTP 200, a complete SSE terminator, no stream
parse error, the expected `tool_calls` finish reason, and exactly 1,024
completion tokens. The benchmark stores raw prompts, responses, and traces
only in a private ignored artifact directory; they are deliberately not
published.

## Input/output inspection

The representative prompt placed the model in an OpenClaw-like coding session
inside `/workspace/queuepilot`. It described an asyncio cancellation race,
required evidence before edits, and advertised `exec`, `read`, and `edit`
tools. The benchmark records the first proposed action but does not execute
it.

Every measured response proposed one parseable `exec` call:

- 43 used `find`; two used `ls`;
- all commands were read-only discovery under `/workspace/queuepilot`;
- 37 included the schema-required `workdir`; and
- eight omitted `workdir`, although their command used the absolute workspace
  path.

The last point is a real schema-compliance miss. Transport success and a
sensible read-only first action do not prove that a full agent trajectory will
complete correctly. Exact-length mode also forces generation through EOS and
is useful for throughput, not semantic grading. The separate sandbox agent
evaluation remains the quality evidence.

## Cold-start and memory trade-off

| Cold-start metric | C8 agent | C32 throughput |
| --- | ---: | ---: |
| Captured graph memory | 1.27 GiB | 2.59 GiB |
| Graph capture time | about 7 s | about 24 s |
| Engine initialization | 93.83 s | 110.76 s |
| Reported KV capacity | 1,417,464 tokens | 1,464,202 tokens |

C8 gives up about 3.2% of the cold-log KV pool. A later live
`cache_config_info` metric disagreed and reported 858,469 tokens
(`kv_cache_max_concurrency=0.8187`), only 72,037 tokens above two configured
393,216-token working contexts. Treat two such contexts as a configuration
ceiling, not a promise that both can be filled while retaining arbitrary
prompt/tool overhead; compaction should act well before that point. Both ranks
started with zero container restarts, no OOM flag, and a process-visible
500,000 soft/hard `nofile` limit.

## Reproduce

Use a private artifact root because request/response bodies and telemetry are
not public-repository material:

```bash
python3 deepseek_v4_bench/benchmark.py \
  --endpoint http://127.0.0.1:8889 \
  --model deepseek-v4-flash \
  --label tp2-dspark-agent-c8-nvfp4-k5-hot-r3 \
  --output-dir "${PRIVATE_ARTIFACT_ROOT}/c8-hot-r3" \
  --concurrency 1 2 4 8 \
  --repeats 3 \
  --prompt-tokens 1024 \
  --output-tokens 1024
```

The benchmark's fixed-length default is intentional here. Use `--honor-eos`
and semantic tests for natural agent behavior.
