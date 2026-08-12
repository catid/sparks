# Three-Spark fabric with two DGX Sparks serving DeepSeek V4 Flash

This repository captures an audited live deployment for serving the pinned
`apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8` revision across two ranks
on a three-node NVIDIA DGX Spark ring, together with reproducible fresh-host
automation. The original `deepseek-ai/DeepSeek-V4-Flash-DSpark` lock remains
available as a reference profile. `cerberus1`
owns the OpenAI-compatible endpoint and supervises both ranks; `cerberus2` is
a headless worker; `cerberus3` completes the physical ring but is not a vLLM
rank. The selected
profile uses TP=2, native DSpark speculative decoding (`k=5`), NVFP4 DS-MLA KV
cache, FlashInfer B12X kernels, thinking mode, and the two logical RoCE links
on the direct C1-P1 to C2-P0 edge. The active scheduler is the C8 agent profile;
the C32 throughput profile remains available for bulk request waves.

The active pair and its installed artifacts were inspected directly. The
fresh-host tooling was also replayed on newly unboxed `cerberus3`: DGX updates,
firmware inspection, host packages, headless boot, performance governor, ring Netplan,
pinned image, and exact checkpoint were validated there. A completely fresh
two-rank serving pair has not yet been rebuilt solely from the public runbook.

The canonical model endpoint is `http://cerberus1.local:8889/v1`. The
dashboard certificate covers both `https://cerberus1.lan` and
`https://cerberus1.local` by default.

## Give an installer temporary sudo access first

The lifecycle scripts are intentionally non-interactive. If Codex or another
automation agent will perform the bootstrap, run this once on **each Spark**
from an attended shell:

```bash
git clone --recurse-submodules https://github.com/catid/sparks.git ~/sparks
cd ~/sparks
scripts/bootstrap-sudo.sh enable
```

This creates `/etc/sudoers.d/90-sparks-bootstrap-nopasswd` and grants the
selected user unrestricted passwordless root access. It is deliberately broad
and temporary. After setup, retain only the narrower Docker policy needed by
the model supervisor and optional ring-maintenance tools, then remove the
bootstrap rule on all three machines:

```bash
cd ~/sparks
scripts/install-docker-sudoers.sh install
scripts/bootstrap-sudo.sh disable
scripts/bootstrap-sudo.sh status
```

Docker access is still root-equivalent. Read
[`docs/SETUP.md`](docs/SETUP.md) before delegating an unattended installation,
and never put API keys, Hugging Face tokens, SSH private keys, or active
OpenClaw state in this public checkout.

## What is reproduced here

| Component | Selected deployment |
| --- | --- |
| Active model | `DeepSeek-V4-Flash-0731-Abliterated-FP8@7d02640c…` |
| Reference model | `DeepSeek-V4-Flash-DSpark@62af8ff…` |
| Container | digest-pinned `ghcr.io/anemll/dspark-vllm-gx10` |
| Parallelism | one TP=2 generation spanning both Sparks; PP=1 |
| Speculation | native probabilistic DSpark, five draft tokens |
| Context | 1,048,576-token ceiling; 8 active scheduler slots |
| API | C1 only, port 8889; canonical `deepseek-v4-flash` alias |
| Fabric | three-node CX-7 ring; production uses the two C1-C2 logical links |
| Recovery | C1 systemd supervisor recycles the complete TP pair; C3 is not a dependency |
| Dashboards | C1 operator telemetry plus a 1424x280 C3 rack display with per-node utilization traces and API-wide live token rate |
| C3 TTS | isolated Audio8 0.6B BF16 OpenAI-compatible service; optional private, operator-approved reference |
| C3 voice | CP900 → pinned Qwen3-ASR 1.7B → Cerberus gate → loopback OpenClaw → C1/C2 DS4F → Audio8 |

The model and container are both pinned and validated before launch. Model
weights remain outside Git and are mounted read-only. The MiaAI-Lab recipe is
kept as a clean, pinned submodule; all local overlays and lifecycle code live
beside it.

The rank-host audit found no unexplained installed drift. It also found no
custom GPU power limit, forced clocks, inference sysctl bundle, preallocated
hugepages, or replacement Linux kernel. All three hosts boot to
`multi-user.target` and GDM remains disabled. C1 and C2 are fully headless;
C3 runs only the small rootless-X kiosk unit needed by its rack display.

## Measured throughput

The active C8 FP8/direct-edge profile was measured for three waves per row
with 1,027–1,030 input tokens and exactly 1,024 output tokens:

| Concurrent requests | Aggregate output tok/s, median | Mean |
| ---: | ---: | ---: |
| 1 | 64.72 | 62.73 |
| 2 | 107.46 | 104.95 |
| 4 | 146.27 | 145.11 |
| 8 | 219.43 | 215.28 |

DFlash draft-token acceptance was 79.96%. RDMA counters proved that only the
two logical links on C1-P1↔C2-P0 carried this run; C3 and the other ring edges
were idle. Forced 1,024-token output makes this a capacity benchmark, not an
agent-quality score. The complete current result and the failed TP3/PP3
compatibility findings are in
[`results/DEEPSEEK_V4_3SPARK_REPORT.md`](results/DEEPSEEK_V4_3SPARK_REPORT.md).

For context, the historical official-NVFP4, pre-ring C32 profile used two
physical C1-C2 cables and produced:

| Concurrent requests | Aggregate output tok/s | Mean post-first-token tok/s/request |
| ---: | ---: | ---: |
| 1 | 71.95 | 73.54 |
| 2 | 100.81 | 53.22 |
| 4 | 140.36 | 36.81 |
| 8 | 188.80 | 26.34 |
| 16 | 283.94 | 19.65 |
| 32 | 381.77 | 14.54 |

These are aggregate server rates, not 381 tok/s for each of 32 users. The live
C8 agent profile is separately qualified at C1-C8; its repeated comparison and
memory trade-off are documented in
[`docs/VLLM_TUNING.md`](docs/VLLM_TUNING.md). The complete original
methodology, comparisons, thermals, network counters, failure cases, and
qualitative agent evaluation are in
[`results/DEEPSEEK_V4_2SPARK_REPORT.md`](results/DEEPSEEK_V4_2SPARK_REPORT.md).

## Start here

- [Fresh three-node fabric / two-rank setup](docs/SETUP.md)
- [Architecture and ownership](docs/ARCHITECTURE.md)
- [ConnectX-7 three-node ring and production TP2 edge](docs/NETWORKING.md)
- [Headless, power, firmware, and host tuning](docs/HOST_TUNING.md)
- [Software inventory](docs/SOFTWARE.md)
- [Container and checkpoint provenance](docs/CONTAINERS.md)
- [vLLM and DSpark tuning](docs/VLLM_TUNING.md)
- [Boot, recovery, dashboard, and routine operations](docs/OPERATIONS.md)
- [Cerberus node 3 rack dashboard](c3_dashboard/README.md)
- [Cerberus node 3 Audio8 TTS](audio8/README.md)
- [Cerberus node 3 always-on voice assistant](voice_assistant/README.md)
- [Move the dashboard to a dedicated Linux host](docs/REMOTE_DASHBOARD.md)
- [Validation and benchmarks](docs/VALIDATION.md)
- [Installed-file map](docs/INSTALLED_ARTIFACTS.md)
- [Public-repository safety policy](PUBLIC_REPOSITORY.md)

The short path, after completing the prerequisites in the setup guide, is:

```bash
# C1
MIA_ENV_FILE=mia-agent.local.env \
  dspark_mia/bin/preflight.sh
MIA_ENV_FILE=mia-agent.local.env \
  scripts/install-dspark-supervisor.sh start

# Health and identity checks
MIA_ENV_FILE=mia-agent.local.env \
  dspark_mia/bin/probe.sh
curl -fsS http://127.0.0.1:8889/v1/models | jq
```

`preflight.sh` is read-only and refuses to pull images, download weights, stop
another workload, or launch a rank. A cold DSpark start normally takes several
minutes for weight loading, warm-up, and CUDA-graph capture.

## Repository layout

- `dspark_mia/`: current pinned DSpark integration, Compose overlay, tests,
  supervisor, and pristine upstream submodule
- `scripts/`: fresh-host bootstrap, networking, model/image provisioning, and
  service installers
- `systemd/`, `netplan/`, `dashboard/`, `security/`, `libexec/`: installed
  artifacts plus portable templates
- `c3_dashboard/`: lightweight three-host collector and 1424x280 rootless-X
  rack kiosk for Cerberus node 3 (`cerberus3`)
- `audio8/`: pinned Audio8 0.6B BF16 TTS container and service wrapper for
  Cerberus node 3 (`cerberus3`); reference media remains private
- `voice_assistant/`: pinned Qwen3-ASR, RAM-only CP900 wake bridge, isolated
  OpenClaw voice agent, and boot-persistent C3 systemd units
- `deepseek_v4_bench/`: fixed-length realistic streaming benchmark
- `deepseek_v4_agent_eval/`: disposable-sandbox coding-agent evaluation
- `bench/`: NCCL/RDMA and lower-level benchmark helpers
- `results/`: reviewed reports and aggregate result files only
- `bin/`, `deepseek-v4/`, `agent_eval/`: earlier experiments and the retired
  Laguna topology, retained for provenance
- `openclaw/`: sanitized configuration for the separate remote OpenClaw
  control host (the Mac Studio, not a Spark), GPT-5.6 Sol verifier routing,
  and its headless service persistence

Generated responses, prompts, reasoning traces, telemetry, logs, credentials,
certificates, model weights, and host state are intentionally excluded. Run
`scripts/check-public.sh --staged` before every public push.
