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

## Fixed three-Spark comparison (port 8893)

`run_mia3_fixed1024.sh` is the frozen apples-to-apples profile for the isolated
three-Spark PP3 trial. It does not start, stop, or reconfigure the service. It
targets `http://127.0.0.1:8893` and fixes all performance-sensitive workload
controls:

The pinned PP3 trial currently produces no endpoint: target-only mode fails
engine initialization after loading weights on the DeepSeek V4 compressed
state-cache stride requirement, and DSpark/DFlash does not support PP. The
runner is retained for testing a future compatible runtime; a startup failure
must not be published as a zero-throughput benchmark row.

- concurrency 1, 2, 4, and 8, in that order;
- one untimed 128-token warm-up wave before each concurrency;
- three measured waves per concurrency;
- chat-aware calibration to 1,024 +/- 12 input tokens;
- exactly 1,024 output tokens (`min_tokens`, `max_tokens`, and `ignore_eos`);
- a realistic coding-agent prompt, max thinking, streaming, and fixed seeds.

Name the runtime and partition in the label so results cannot be mistaken for
another configuration:

```bash
cd "$HOME/sparks"
deepseek_v4_bench/run_mia3_fixed1024.sh \
  --label pp3-target-auto
```

Use labels such as `pp3-target-14-15-14`, `pp3-target-16-15-12`, or
`pp3-dspark-15-15-13` only when they describe the service actually listening on
8893. The runner discovers the served model from `/v1/models`; it does not
infer or change the live PP/DFlash configuration.

Raw requests, byte-exact SSE, reconstructed reasoning/tool calls, response
headers, metrics snapshots, and the runner log default to a mode-0700 directory
under `${XDG_STATE_HOME:-$HOME/.local/state}/sparks/deepseek-v4-bench`. They do
not enter this checkout. Override that root with `--artifact-root` or
`MIA3_BENCH_ARTIFACT_ROOT`; an override inside the checkout is accepted only
when Git confirms that the path is ignored. `/.bench-private/` is provided as
an explicit local fallback:

```bash
deepseek_v4_bench/run_mia3_fixed1024.sh \
  --label pp3-target-auto \
  --artifact-root "$PWD/.bench-private"
```

After any run that produced `summary.json`, the runner creates
`summary.public.json` and `summary.public.csv`. These are built with an
allowlist and omit the endpoint, discovered model/path, source label, errors,
prompts, response text, reasoning, headers, request IDs, and artifact paths.
Use `--public-dir DIR` to place an additional publish-safe copy elsewhere. The
JSON includes all three per-wave measurements, aggregate concurrency rows,
token calibration range, DFlash counters when exposed, and a SHA-256 of the
private source summary.

Validate the exact matrix without contacting the endpoint or writing files:

```bash
deepseek_v4_bench/run_mia3_fixed1024.sh --label check --dry-run
```

For a fair configuration comparison, change only the server configuration and
the descriptive label. Keep this runner, prompt/output lengths, seed, ordering,
and machine thermal/power conditions unchanged. Compare
`aggregate_output_tokens_per_s` for total service capacity and TTFT plus
`aggregate_output_tokens_per_s_per_user` for interactive behavior. Inspect the
private reconstructed responses separately for semantic/tool-call quality;
throughput alone does not establish agent quality.
