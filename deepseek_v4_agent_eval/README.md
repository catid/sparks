# DeepSeek V4 qualitative OpenClaw-style evaluation

This harness tests whether DeepSeek V4 Flash NVFP4 can complete realistic
coding/debugging work through an OpenClaw-style `exec` tool loop. It is
separate from the throughput benchmark so long reasoning and tool interaction
do not distort batch token-rate measurements.

It does **not** start OpenClaw, change any service, reboot a Spark, or execute a
model command on the host. Running the script without `--execute` is an inert
validation pass.

## Request settings

Every completion request fixes these fields:

```json
{
  "temperature": 1.0,
  "top_p": 1.0,
  "reasoning_effort": "max",
  "chat_template_kwargs": {
    "thinking": true,
    "enable_thinking": true,
    "reasoning_effort": "max"
  }
}
```

Execution has no guessed context default. Pass the exact auto-fit context
reported by the running vLLM server with `--context-window`. Unless
`--max-tokens` is supplied explicitly, the harness mirrors the completion's
messages, tools, and thinking template arguments through vLLM's authoritative
`/v1/chat/completions/render` endpoint before every turn, then requests the
exact remaining live context as that turn's output ceiling. This preserves the
largest valid allowance as tool history grows instead of relying on a guessed
fixed reserve. The harness also verifies `--context-window` against
`/v1/models`; the argument cannot enlarge an already-running server.

## Isolation and command policy

Each task gets exactly one randomly named, disposable Docker container. The
same container handles all model tool calls and the hidden grade, then the
harness removes it. Its controls include:

- no network;
- read-only root filesystem;
- all Linux capabilities dropped and `no-new-privileges`;
- no Docker socket, host PID namespace, host credentials, or host environment;
- one writable bind mount containing only that task's generated workspace,
  plus a disposable `/tmp` tmpfs;
- PID, memory, CPU, file-size, command-time, and output limits;
- fixed argv-based `docker exec` invocation, never `shell=True` on the host;
- a conservative validator rejecting host paths, parent traversal, network,
  privilege/container tools, service/power commands, and background jobs.
- forced container removal with a recorded cleanup failure if Docker cannot
  prove that the disposable sandbox is gone.

The script needs an already-local `python:3.12-slim` image by default. The
explicit `--allow-image-pull` flag permits a pull if it is missing.

## Validate without contacting anything

```bash
cd "$HOME/sparks/deepseek_v4_agent_eval"
python3 run_agent_eval.py
python3 -m unittest -v test_harness.py
```

## Run later, once the vLLM endpoint is ready

Read the exact auto-fit context from the vLLM startup log, then use the model ID
served by vLLM:

```bash
cd "$HOME/sparks/deepseek_v4_agent_eval"
AUTO_FIT_CONTEXT=REPLACE_WITH_EXACT_VLLM_STARTUP_VALUE
python3 run_agent_eval.py \
  --execute \
  --endpoint http://127.0.0.1:8889 \
  --model deepseek-v4-flash-dspark-mia-throughput \
  --context-window "${AUTO_FIT_CONTEXT}"
```

Omitting `--max-tokens` in this example derives the exact maximum separately on
every turn. Set it only when deliberately imposing a smaller per-turn ceiling:

```bash
python3 run_agent_eval.py \
  --execute \
  --endpoint http://127.0.0.1:8000 \
  --model deepseek-v4-flash-nvfp4 \
  --context-window "${AUTO_FIT_CONTEXT}" \
  --max-tokens 16384 \
  --tasks retry_queue_debug
```

The API key is unnecessary for the local vLLM endpoint. If one is ever needed,
pass only the environment-variable name, for example
`--api-key-env LOCAL_VLLM_KEY`; the value is not written to artifacts.

## Evidence retained

Each timestamped run keeps:

- `RUN_CONFIG.json` and each task's `manifest.json`, including the complete
  system prompt, user prompt, tool schema, input hashes, thinking settings, and
  sandbox policy;
- `events.jsonl`, with every full API request, unmodified API response,
  exact authoritative-render count and per-turn output allowance, reasoning
  field, tool call, exact command, complete result (up to the enforced 16 MiB
  safety ceiling), final answer, error, and grade;
- `trajectory.json`, a readable turn-by-turn view;
- `summary.json`, grading and token/timing totals;
- the final task workspace for direct inspection.

Tests and adversarial fixture files are hashed before the agent starts and
verified both before and after hidden grading. Hidden graders use isolated
Python startup and must emit an exact harness-required success marker. A task
cannot pass if protected content changes, the marker is absent, the final
response is truncated or empty, sandbox removal fails, or any model command
triggers a policy violation.

## Task coverage

- `ledger_bugfix` checks careful repository inspection, exact decimal handling,
  input validation, implementation, and test evidence.
- `retry_queue_debug` adds ordering, lease-boundary, persistence, atomic-replace,
  and prompt-injection resistance.
- `worker_pool_cancel` exercises asynchronous cancellation semantics,
  concurrency, idempotence, focused/full-test execution, and cleanup evidence.

Together they exercise the OpenAI/Hermes function-call loop and the practical
inspect-edit-test-report behavior expected from OpenClaw. They are a coding
agent evaluation, not a substitute for testing OpenClaw's messaging, browser,
memory, scheduler, or credential integrations.
