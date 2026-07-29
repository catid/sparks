# Two DGX Sparks serving DeepSeek V4 Flash

This repository captures an audited live deployment for serving
`deepseek-ai/DeepSeek-V4-Flash-DSpark` across two NVIDIA DGX Sparks, together
with reproducible fresh-host automation. Spark 1 owns the OpenAI-compatible
endpoint and supervises both ranks; Spark 2 is a headless worker. The selected
profile uses TP=2, native DSpark speculative decoding (`k=5`), NVFP4 DS-MLA KV
cache, FlashInfer B12X kernels, thinking mode, and four logical RoCE rails
carried by two ConnectX-7 cables. The active scheduler is the C8 agent profile;
the C32 throughput profile remains available for bulk request waves.

The active pair and its installed artifacts were inspected directly. The new
portable renderers/installers have static, unit, and non-mutating integration
coverage, but have not yet been replayed as a bare-metal installation on a
third fresh pair.

The current endpoint is `http://spark1.lan:8889/v1`. The optional dashboard is
available through Nginx at `http://spark1.lan` and `https://spark1.lan`.

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
the model supervisor, then remove the bootstrap rule on both machines:

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
| Model | `DeepSeek-V4-Flash-DSpark@62af8ff…` |
| Container | digest-pinned `ghcr.io/anemll/dspark-vllm-gx10` |
| Parallelism | one TP=2 generation spanning both Sparks; PP=1 |
| Speculation | native probabilistic DSpark, five draft tokens |
| Context | 1,048,576-token ceiling; 8 active scheduler slots |
| API | Spark 1 only, port 8889; canonical `deepseek-v4-flash` alias |
| Fabric | four 200 Gb/s, MTU-9000 RoCE interfaces |
| Recovery | Spark 1 systemd supervisor recycles the complete TP pair |
| Dashboard | GPU/CPU/SoC/NVMe/CX-7 thermals, memory, vLLM and RDMA history |

The model and container are both pinned and validated before launch. Model
weights remain outside Git and are mounted read-only. The MiaAI-Lab recipe is
kept as a clean, pinned submodule; all local overlays and lifecycle code live
beside it.

The live two-Spark audit found no unexplained installed drift. It also found no
custom GPU power limit, forced clocks, inference sysctl bundle, preallocated
hugepages, or replacement Linux kernel. Both hosts boot to
`multi-user.target`; the stock NVIDIA X configuration remains installed for
easy rollback, but GDM/X is not running.

## Measured throughput

The native DSpark C32 throughput baseline was measured with realistic coding-agent
prompts calibrated to 1,024 ± 12 input tokens, maximum reasoning, thinking
enabled, and exactly 1,024 generated tokens per request:

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

- [Fresh two-host setup](docs/SETUP.md)
- [Architecture and ownership](docs/ARCHITECTURE.md)
- [ConnectX-7 and four-rail RoCE](docs/NETWORKING.md)
- [Headless, power, firmware, and host tuning](docs/HOST_TUNING.md)
- [Software inventory](docs/SOFTWARE.md)
- [Container and checkpoint provenance](docs/CONTAINERS.md)
- [vLLM and DSpark tuning](docs/VLLM_TUNING.md)
- [Boot, recovery, dashboard, and routine operations](docs/OPERATIONS.md)
- [Validation and benchmarks](docs/VALIDATION.md)
- [Installed-file map](docs/INSTALLED_ARTIFACTS.md)
- [Public-repository safety policy](PUBLIC_REPOSITORY.md)

The short path, after completing the prerequisites in the setup guide, is:

```bash
# Spark 1
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
- `deepseek_v4_bench/`: fixed-length realistic streaming benchmark
- `deepseek_v4_agent_eval/`: disposable-sandbox coding-agent evaluation
- `bench/`: NCCL/RDMA and lower-level benchmark helpers
- `results/`: reviewed reports and aggregate result files only
- `bin/`, `deepseek-v4/`, `agent_eval/`: earlier experiments and the retired
  Laguna topology, retained for provenance
- `openclaw/`: sanitized, validated third-host OpenClaw configuration,
  GPT-5.6 Sol verifier routing, and headless macOS persistence

Generated responses, prompts, reasoning traces, telemetry, logs, credentials,
certificates, model weights, and host state are intentionally excluded. Run
`scripts/check-public.sh --staged` before every public push.
