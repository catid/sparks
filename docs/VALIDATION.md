# Validation and benchmarking

Validation is layered so source mistakes, artifact drift, network faults,
startup failures, transport regressions, throughput changes, and semantic
model failures are not collapsed into one “server is up” result.

## 1. Offline source checks

From the repository root, check shell syntax and deterministic tests:

```bash
find bin scripts dashboard dspark_mia/bin dspark_mia/tests \
  dspark_mia3/bin dspark_mia3/tests bench openclaw tests \
  -type f -name '*.sh' -print0 |
  xargs -0 -r -n1 bash -n

shellcheck \
  bin/*.sh scripts/*.sh dspark_mia/bin/*.sh dspark_mia3/bin/*.sh \
  dspark_mia/tests/*.sh dspark_mia3/tests/*.sh bench/*.sh \
  openclaw/*.sh tests/*.sh

MIA_ENV_FILE=mia-agent.local.env \
  ./dspark_mia/bin/validate-static.sh

MIA_ENV_FILE=mia-agent.local.env \
  ./scripts/install-dspark-supervisor.sh verify
./scripts/install-dashboard.sh verify --web
./scripts/install-dashboard-probe.sh verify
./scripts/install-dashboard.sh verify --remote-collector

(cd dspark_mia && \
  MIA_ENV_FILE=mia-throughput.env ./tests/test-profile-selection.sh)
(cd dspark_mia && ./tests/test-model-lock-selection.sh)
(cd dspark_mia && ./tests/test-profile-renderer.sh)
(cd dspark_mia && ./tests/test-model-catalog.sh)
(cd dspark_mia && \
  MIA_ENV_FILE=mia-throughput.env ./tests/test-start-timeout.sh)
(cd dspark_mia && \
  MIA_ENV_FILE=mia-throughput.env ./tests/test-supervisor.sh)
./tests/test-cx7-ring-layout.sh
./tests/test-cx7-installer-cleanup.sh
./dspark_mia3/tests/run.sh
./bench/tests/test_ring_nccl_static.sh
```

`validate-static.sh` renders both rank configurations without pulling or
starting a container. It verifies the upstream commit/tree, image digest,
read-only checkpoint mount, project isolation, rank placement, TP2/PP1,
DSpark k5, thinking mode, scheduler/graph relationship, ports, rank-specific
direct-edge HCAs, and management-plane control interfaces.

Run the Python unit suites in an environment with their test dependencies.
Install the benchmark's audited direct dependency with
`pip install -r deepseek_v4_bench/requirements.txt`; the runtime-specific KV
test also needs the pinned PyTorch/vLLM environment:

```bash
python3 -m unittest -v dashboard.tests.test_dashboard
(cd deepseek_v4_bench && python3 -m unittest -v)
(cd deepseek_v4_agent_eval && python3 -m unittest -v)
python3 deepseek-v4/test_dspark_overlay.py
```

Run the runtime-specific test with the same Python stack used by vLLM:

```bash
/path/to/vllm-python deepseek-v4/test_vllm_dsv4_dflash_kv.py
```

Before publishing changes to the public repository:

```bash
git status --short
git add -- path/to/reviewed-file another/reviewed-file
./scripts/check-public.sh --staged
```

Review the staged filename list and diff as well. The safety checker is a
backstop, not permission to commit credentials, private SSH material, raw
agent trajectories, runtime state, or credential-bearing site environment
files. The explicitly allowlisted DSpark profiles contain no credentials.

## 2. Artifact and fabric readiness

After generating and synchronizing the selected local profile:

```bash
MIA_ENV_FILE=mia-agent.local.env ./dspark_mia/bin/sync-worker.sh
MIA_ENV_FILE=mia-agent.local.env ./dspark_mia/bin/preflight.sh
```

The sync changes only the worker's pinned integration tree. Preflight itself
is non-downloading and does not start or stop services. It checks:

- identical selected-profile hashes and clean upstream commits on both hosts;
- both production-edge logical links, MTU 9000, exact C1/C2 addresses, active
  RDMA, and peer reachability;
- free API and rendezvous ports;
- absence of another vLLM GPU workload;
- the exact local image digest on both nodes; and
- the complete, revision-pinned checkpoint on both nodes.

Because it requires free ports and no running vLLM process, preflight is for a
cold deployment window. Do not stop a healthy production generation merely
to run it.

The fabric readiness check can be run separately and safely:

```bash
CX7_NODE_ROLE=cerberus1 ./bin/wait-cx7-ready.sh --check-once --scope tp2
ssh cerberus2 \
  'cd /path/to/sparks && CX7_NODE_ROLE=cerberus2 ./bin/wait-cx7-ready.sh --check-once --scope tp2'

# Independently validate both neighbor edges on every ring node:
CX7_NODE_ROLE=cerberus1 ./bin/wait-cx7-ready.sh --check-once --scope ring
ssh cerberus2 \
  'cd /path/to/sparks && CX7_NODE_ROLE=cerberus2 ./bin/wait-cx7-ready.sh --check-once --scope ring'
ssh cerberus3 \
  'cd /path/to/sparks && CX7_NODE_ROLE=cerberus3 ./bin/wait-cx7-ready.sh --check-once --scope ring --c3-port-map c3-p0-to-c1'
```

Ring readiness proves the exact IP, carrier, speed, MTU, RDMA state, and peer
ping matrix; it does not prove a three-rank collective. C3 is reserved for an
independent model workload in the selected deployment, so no three-rank
collective is required. The retained `bench/run_verify_ring_nccl230.sh`
experiment requires intentionally stopping production, physically selecting
NVIDIA's crossed C3 cable orientation, and switching C3 to `c3-p0-to-c2`; it
is not part of routine validation.

## 3. Live functional checks

Once the service is ready:

```bash
curl -fsS http://127.0.0.1:8889/health
curl -fsS http://127.0.0.1:8889/v1/models | jq .
MIA_ENV_FILE=mia-agent.local.env ./dspark_mia/bin/probe.sh
curl -fsS http://127.0.0.1:8889/metrics | head
```

`probe.sh` is stronger than an HTTP 200: it checks both labelled rank
containers, OOM state, host boot IDs, container start times, and the exact
served model. Preserve its fingerprint before and after recovery tests.

A small thinking/tool-call smoke request should be inspected, not just timed.
Use a local placeholder model name discovered from `/v1/models`:

```bash
model="$(
  curl -fsS http://127.0.0.1:8889/v1/models |
    jq -r '.data[0].id'
)"
curl -fsS http://127.0.0.1:8889/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg model "${model}" '{
    model: $model,
    messages: [{
      role: "user",
      content: (
        "Inspect this shell plan for safety, then list regular files under " +
        "the current directory without changing anything."
      )
    }],
    tools: [{
      type: "function",
      function: {
        name: "exec",
        description: "Run one read-only shell command in a disposable sandbox.",
        parameters: {
          type: "object",
          properties: {command: {type: "string"}},
          required: ["command"],
          additionalProperties: false
        }
      }
    }],
    tool_choice: "auto",
    reasoning_effort: "max",
    max_tokens: 2048,
    temperature: 1.0,
    top_p: 1.0
  }')" | jq .
```

This request does not execute the proposed command. Check reasoning/content,
finish reason, usage, and any tool-call arguments before treating it as a
functional pass.

## 4. Proving the production RDMA edge

Ordinary Ethernet byte graphs are insufficient because RoCE data uses RDMA
hardware counters. Capture counters on both hosts before and after a
substantial serving workload:

```bash
state_dir="${XDG_STATE_HOME:-${HOME}/.local/state}/sparks/rail-proof"
mkdir -p "${state_dir}"
python3 bench/rdma_counters.py --save "${state_dir}/cerberus1-before.json"
ssh cerberus2 \
  'cd /path/to/sparks && python3 bench/rdma_counters.py' \
  >"${state_dir}/cerberus2-before.json"
```

Run a fixed, repeatable inference wave, then take matching `after` snapshots.
Capture and compare each host:

```bash
python3 bench/rdma_counters.py --save "${state_dir}/cerberus1-after.json"
ssh cerberus2 \
  'cd /path/to/sparks && python3 bench/rdma_counters.py' \
  >"${state_dir}/cerberus2-after.json"

python3 bench/rdma_counters.py \
  --before "${state_dir}/cerberus1-before.json" \
  --after "${state_dir}/cerberus1-after.json"
python3 bench/rdma_counters.py \
  --before "${state_dir}/cerberus2-before.json" \
  --after "${state_dir}/cerberus2-after.json"
```

For production TP2, only C1 P1 (`*f1`) and C2 P0 (`*f0`) should have
substantial RX and TX deltas. The other ring ports may remain idle. Directional
totals should be plausible across peers, and error/discard totals should not
increase. The helper above records bytes; the dashboard provides both
hardware-source byte rates and interface
error totals for ongoing observation.

`bench/run_verify_multirail_nccl230.sh` is retained as a historical
destructive-to-capacity all-reduce check for the earlier host venv plus local
NCCL build. It is guarded and is not a fresh-install prerequisite; the
selected deployment gets NCCL 2.30.7 from the pinned container. Do not recreate
that old host build merely to run the helper. For the current setup, prove the
mapped container library as described in [SOFTWARE.md](SOFTWARE.md), then use
the serving workload and paired RDMA-counter deltas above. Any standalone
all-reduce still requires a maintenance window with vLLM stopped.

## 5. Realistic 1,024-in/1,024-out matrix

[`deepseek_v4_bench/benchmark.py`](../deepseek_v4_bench/benchmark.py) uses a
compact coding-agent conversation with shell/read/edit tool schemas. It
calibrates rendered chat prompts to approximately 1,024 tokens, sends maximum
reasoning with thinking enabled, and records byte-exact SSE plus reconstructed
reasoning, text, usage, and tool calls. The harness advertises tools but never
executes a model-generated tool call.

First validate request construction without network calls:

```bash
python3 deepseek_v4_bench/benchmark.py --dry-run
```

Run the comparable matrix into a new private artifact directory:

```bash
artifact_root="${XDG_STATE_HOME:-${HOME}/.local/state}/sparks"
mkdir -p "${artifact_root}"
run_dir="$(mktemp -d "${artifact_root}/dsv4-fixed1024.XXXXXX")"
python3 deepseek_v4_bench/benchmark.py \
  --endpoint http://127.0.0.1:8889 \
  --label tp2-dspark-nvfp4-k5 \
  --output-dir "${run_dir}" \
  --concurrency 1 2 4 8 \
  --repeats 1 \
  --prompt-tokens 1024 \
  --output-tokens 1024
```

Install `deepseek_v4_bench/requirements.txt` in the Python environment first.
The default forces exact completion length with vLLM's `min_tokens`,
`max_tokens`, and `ignore_eos`; this is appropriate for throughput
comparability but not agent-quality judgment. It runs a short untimed warm-up
before each measured wave.

The active agent profile schedules at most eight sequences. To measure native
C16/C32 batching rather than queued work, first perform a coordinated switch
to `mia-throughput.local.env`, then add `16 32` to the matrix. Switch back to
the agent profile after the throughput experiment.

Review at least:

```bash
jq . "${run_dir}/summary.json"
column -s, -t <"${run_dir}/summary.csv" | less -S
find "${run_dir}/requests" "${run_dir}/responses" -type f | sort | less
```

Check all HTTP statuses, exact prompt/output usage, finish reasons, TTFT,
end-to-end latency, aggregate tokens/s, per-user rate, speculative acceptance,
and the actual requests and responses. A high aggregate rate with malformed
tool calls, repetitive reasoning, or incorrect code is a failed result.

Raw prompts, reasoning, tool calls, telemetry, and host details may be
sensitive. They are intentionally excluded from this public repository.
Publish only a manually reviewed, sanitized summary.

For a lightweight tokenizer-independent saturation check, the synthetic
completion harness sends distinct token-ID prompts:

```bash
python3 bench/bench_serving.py \
  --endpoints http://127.0.0.1:8889 \
  --label tp2-dspark-synthetic \
  --output "${run_dir}/synthetic.json" \
  --concurrency 1 2 4 8 16 32 \
  --input-tokens 1024 \
  --output-tokens 1024
```

That test is useful for server mechanics, not model quality, because its token
IDs do not form a realistic coding prompt.

## 6. Natural agent-quality trials

For realistic behavior, allow EOS and give the model a large output allowance:

```bash
artifact_root="${XDG_STATE_HOME:-${HOME}/.local/state}/sparks"
mkdir -p "${artifact_root}"
natural_dir="$(mktemp -d "${artifact_root}/dsv4-natural.XXXXXX")"
python3 deepseek_v4_bench/benchmark.py \
  --endpoint http://127.0.0.1:8889 \
  --label tp2-dspark-natural \
  --output-dir "${natural_dir}" \
  --concurrency 1 \
  --repeats 1 \
  --prompt-tokens 1024 \
  --output-tokens 32768 \
  --honor-eos
```

Throughput is secondary here. Evaluate whether the response understood the
task, chose safe tools, used valid arguments, modified the right files, tested
the semantic invariant, stopped, and provided an accurate final report.

The included agent harness runs model-proposed shell interactions only inside
a disposable sandbox with a read-only root, dropped capabilities, no network,
no Docker socket, and no host credentials. Even then, passing visible tests is
not sufficient. The recorded 24-turn max-context trial passed its tests but
violated the cancellation invariant and never produced a final answer. See
[`DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md`](../results/DEEPSEEK_V4_DSPARK_AGENT_EVAL_MAX.md)
before considering OpenClaw or Hermes integration.

The Spark validation steps do not install OpenClaw. The separately validated
third-host deployment and its local-model/Sol verifier smoke checks are
documented in [`openclaw/README.md`](../openclaw/README.md).

## 7. Recovery, limits, and thermals

Use the exact-container failure injection in
[OPERATIONS.md](OPERATIONS.md#non-reboot-recovery-test). A pass requires both
container fingerprints to change, the same model to return, and no manual
rank-1 intervention.

The active C8 cold generation passed Docker and process-visible checks for a
`nofile` soft/hard limit of 500,000 on both ranks. Recheck after every
coordinated reload because repository validation cannot retroactively change a
running container's limits.

During cold capture and the full matrix, monitor:

- C1 and C2 GPU, CPU-cluster, SoC, NVMe, and ConnectX-7 temperatures;
- GPU power/utilization/clock;
- free unified memory, swap, and vLLM RSS;
- service restarts, OOM state, and kernel/NVRM errors; and
- every RDMA rail's byte and error counters.

The dashboard is the convenient live view, but retain timestamped benchmark
artifacts and relevant journal excerpts when establishing a new baseline.
