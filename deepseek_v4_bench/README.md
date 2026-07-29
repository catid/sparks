# DeepSeek V4 streaming benchmark

This harness measures one OpenAI-compatible vLLM endpoint with parallel request
waves of 1, 2, 4, 8, 16, and 32. It uses a compact coding-agent prompt modeled
on an OpenClaw terminal workflow. The advertised `exec`, `read`, and `edit`
tools are schemas only: the harness never executes a model-generated tool call.

The comparable matrix uses:

- a chat-aware `/tokenize` calibration to approximately 1,024 input tokens;
- `reasoning_effort=max` with thinking enabled;
- `temperature=1.0` and `top_p=1.0`;
- streaming responses with final usage;
- exactly 1,024 requested/generated tokens by setting vLLM
  `min_tokens=max_tokens=1024` and `ignore_eos=true`;
- one short untimed warm-up wave before each measured concurrency.

Run it from a Python environment with `aiohttp`. Keep raw request/response
artifacts outside the public checkout:

```bash
cd "$HOME/sparks"
python3 -m venv .venv-bench
.venv-bench/bin/pip install -r deepseek_v4_bench/requirements.txt
artifact_root="${XDG_STATE_HOME:-${HOME}/.local/state}/sparks"
mkdir -p "${artifact_root}"
run_dir="$(mktemp -d "${artifact_root}/dsv4-tp2-dspark.XXXXXX")"
.venv-bench/bin/python deepseek_v4_bench/benchmark.py \
  --endpoint http://127.0.0.1:8889 \
  --label tp2-dspark-nvfp4-k5 \
  --output-dir "${run_dir}"
```

The output directory must be new or empty. If the local server requires an API
key, place it in `VLLM_API_KEY`; its value is used as an Authorization header
but never written to disk. `--model` can bypass `/v1/models`.

Every request has four audit artifacts:

- `requests/*.request.json`: complete body and non-secret headers;
- `responses/*.response.sse`: byte-exact raw SSE;
- `responses/*.events.jsonl`: parsed SSE events with receive timestamps;
- `responses/*.response.json`: usage, finish reasons, reconstructed reasoning,
  content, and tool calls.

`summary.csv` and `summary.json` contain aggregate output throughput, per-user
throughput, request decode rates, TTFT, E2E latency, actual token counts, and
finish reasons. `requests.csv` retains every request result. Prometheus
snapshots and speculative-decoding counter deltas are included when `/metrics`
is available.

For a network-free construction check:

```bash
cd "$HOME/sparks"
.venv-bench/bin/python deepseek_v4_bench/benchmark.py --dry-run
```

Run the unit tests without a server:

```bash
cd "$HOME/sparks/deepseek_v4_bench"
../.venv-bench/bin/python -m unittest -v
```

`--honor-eos` is available for natural agent-quality trials, but it should not
be used for the fixed-length throughput matrix because early stops make rows
incomparable.
