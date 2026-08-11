# Installed-artifact inventory

This file maps the two-rank installation and three-node ring back to repository sources.
It distinguishes the active DSpark deployment from retired experiments and
from private runtime state that must never be committed.

Paths below use:

- `REPO_ROOT`: this checkout under the service account's home;
- `SPARK_USER`: that service account; and
- `MODEL_DIR`: the pinned model directory outside Git.

The audited checkout happened to use a particular user and absolute path.
Those values are installation details, not public defaults.

## Active ownership

| Host | Active project responsibility |
| --- | --- |
| Cerebrus 1 | DSpark supervisor, TP rank 0/API, TP2-edge readiness, dashboard, optional Nginx |
| Cerebrus 2 | TP rank 1 container controlled from C1; no autonomous model supervisor |
| Cerebrus 3 | physical ring member, independent rack telemetry/kiosk and Audio8 TTS; no rank in the active DSpark service |

On C1, `dgx-spark-dspark-mia.service` and
`dgx-spark-laguna-dashboard.service` were enabled and active. The rail gate is
a static oneshot and normally appears inactive after a successful check.

C2's active rank was a Compose container, not an enabled systemd rank
unit. This is intentional: C1 must replace the complete TP generation
when either rank fails.

The current `dgx-spark-cx7-ready.service` exists only as C1's non-resident TP2
startup dependency and is rendered from the `.service.in` template. A static
unit with the same name remains installed and audited on C2 from the retired
rank-service architecture. Mia never starts that C2 unit; preflight/start
instead invokes `bin/wait-cx7-ready.sh --scope tp2` over SSH before launching
C2's Compose rank. Do not mistake the legacy C2 copy for a current
per-rank service dependency.

## systemd units

| Installed unit | Host(s) | Repository source | Audited role/state |
| --- | --- | --- | --- |
| `dgx-spark-dspark-mia.service` | C1 | audited concrete unit in `systemd/dgx-spark-dspark-mia.service`; portable template in `systemd/dgx-spark-dspark-mia.service.in` | active/enabled; current orchestrator |
| `dgx-spark-cx7-ready.service` | C1 | portable template in `systemd/dgx-spark-cx7-ready.service.in` | current static TP2 readiness dependency; normally inactive after each successful start check |
| `dgx-spark-cx7-ready.service` | C2 | legacy installed artifact; the current checked-in concrete unit is C1-specific | static/inactive; retained from the retired rank-service path and unused by Mia |
| `dgx-spark-laguna-dashboard.service` | Spark 1 | `systemd/dgx-spark-laguna-dashboard.service` | active/enabled |
| `dgx-spark-c3-dashboard.service` | C3 | `c3_dashboard/systemd/dgx-spark-c3-dashboard.service.in` | independent three-host collector; intended active/enabled |
| `dgx-spark-c3-kiosk.service` | C3 | `c3_dashboard/systemd/dgx-spark-c3-kiosk.service.in` | rootless-X rack display; intended active/enabled |
| `cerebrus3-audio8.service` | C3 | `systemd/cerebrus3-audio8.service.in` | isolated Audio8 API; intended active/enabled, with no playback loop |
| `dgx-spark-deepseek-v4-rank0.service` | Spark 1 | `systemd/dgx-spark-deepseek-v4-rank0.service` | disabled/inactive legacy deployment |
| `dgx-spark-deepseek-v4-rank1.service` | Spark 2 | `systemd/dgx-spark-deepseek-v4-rank1.service` | static/inactive legacy deployment |
| `dgx-spark-laguna-vllm-agent.service` | both | `systemd/dgx-spark-laguna-vllm-agent.service` | disabled legacy Laguna replica |
| `dgx-laguna-router.service` | Spark 1 | `systemd/dgx-laguna-router.service` | disabled/inactive legacy router |
| `dgx-laguna-router-front.service` | Spark 1 | `systemd/dgx-laguna-router-front.service` | disabled/inactive legacy streaming front |

Rendered active units install under `/etc/systemd/system`. The templates
replace the user, group, home, and repository path; do not copy the audited
absolute paths into a new host.

A timestamped old supervisor unit remained on Spark 1 as an administrative
backup. Such `*.before-*` or `*.oneshot-*` files are local rollback evidence,
not source artifacts, and should not be copied into the public repository or
installed on a fresh host.

NVIDIA's `dgx-*` platform/OOBE/dashboard units under `/usr/lib` or `/etc` are
vendor services, not supplied by this repository. Do not remove them just
because their names share a prefix.

## Host configuration

| Installed path | Host(s) | Public source/template | Notes |
| --- | --- | --- | --- |
| `/etc/netplan/40-cx7.yaml` | all three | corresponding `netplan/cerebrus*-40-cx7.yaml` | exact ring-edge configuration |
| `/etc/default/dgx-spark-laguna-dashboard` | Spark 1 | derive from `dashboard/dashboard.env.example` | mode 0600; live LAN/auth values stay private |
| `/etc/default/dgx-spark-c3-dashboard` | C3 | derive from `c3_dashboard/dashboard.env.example` | mode 0600; node/key paths stay host-local |
| `/etc/default/cerebrus3-audio8` | C3 | optional local file | mode 0600; may name the private approved-reference directory |
| `/etc/nginx/sites-available/dgx-spark-dashboard` | Spark 1 | `dashboard/nginx-spark1-dashboard.conf` | enabled by symlink in `sites-enabled` |
| `/etc/nginx/ssl/cerebrus1.lan.crt` | C1 | generated by `bin/install-dashboard-web.sh` | default local/self-signed certificate; follows `DASHBOARD_WEB_HOST` |
| `/etc/nginx/ssl/cerebrus1.lan.key` | C1 | generated locally | default private key; never commit or copy into docs |
| `/etc/dgx-spark-deepseek-v4.env` | both | `systemd/dgx-spark-deepseek-v4.env.example` | legacy rank-service profile, not active DSpark profile |
| `/etc/dgx-spark-laguna-vllm-agent.conf` | both | `systemd/dgx-spark-laguna-vllm-agent.conf.example` | legacy replica override |
| `/etc/X11/xorg.conf` | all three | NVIDIA's `nvidia-conf-xconfig.service` | vendor-generated, retained for GUI rollback |

The active DSpark profile is selected from `dspark_mia/` and synchronized to
the matching checkout path on Spark 2. It contains topology and runtime
values, not provider credentials. When adapting it, keep the image/model pins
and inference values identical on both nodes while changing user-specific
paths through the documented profile-generation/setup flow.

The audited Nginx certificate is site-local. A self-signed certificate
protects transport but does not establish public trust; clients need a local
trust decision or a certificate from the site's CA.

## Repository executables

No project executable needs to be copied to an untracked `/usr/local/bin`.
The active units execute version-controlled paths directly:

| Function | Source |
| --- | --- |
| ring/TP2-edge readiness | `bin/wait-cx7-ready.sh` |
| DSpark lifecycle and supervisor | `dspark_mia/bin/` |
| Compose behavior | upstream submodule plus `dspark_mia/compose.mia.override.yml` |
| dashboard | `dashboard/run-dashboard.sh`, `dashboard/server.py`, `dashboard/static/` |
| C3 rack telemetry and kiosk | `c3_dashboard/server.py`, `c3_dashboard/kiosk.py`, `c3_dashboard/scripts/` |
| C3 Audio8 API | `audio8/`, `scripts/install-audio8.sh` |
| optional remote dashboard probe | `dashboard/remote-probe.sh`, installed by `scripts/install-dashboard-probe.sh` |
| benchmarks | `bench/`, `deepseek_v4_bench/`, `deepseek_v4_agent_eval/` |

Older rank-control helpers under `bin/`, `libexec/`, `security/`, and the
legacy systemd files document the previous deployment. They are not part of
the active Mia supervisor path.

The dashboard may be moved later without changing the model installation.
`dashboard/dashboard.remote.env.example` and
`systemd/dgx-spark-laguna-dashboard.service.in` render the third-host service;
the fixed probe installs as
`/usr/local/libexec/dgx-spark-dashboard-probe` on each Spark. None of those
optional remote-placement artifacts were installed during this two-host
audit. See [`REMOTE_DASHBOARD.md`](REMOTE_DASHBOARD.md).

Spark 2 still had the previous deployment's restricted controller installed:

| Installed Spark 2 artifact | Repository source | Status |
| --- | --- | --- |
| `/usr/local/libexec/dgx-spark-deepseek-v4-rank1-control` | `libexec/dgx-spark-deepseek-v4-rank1-control` | installed, audited byte-identical, inactive for Mia |
| `/etc/sudoers.d/dgx-spark-deepseek-v4-rank1-control` | `security/dgx-spark-deepseek-v4-rank1-control.sudoers` | installed, audited byte-identical, permits only three fixed legacy unit operations |
| restricted entry in Spark 2's `authorized_keys` | installed by `bin/install-deepseek-v4-rank1-control.sh` | forced command, source restricted to rail-0 Spark 1; key material stays private |

The wrapper accepts only opaque status, restart, and stop request tokens for
the legacy rank-1 unit. Its sudo policy grants literal `reset-failed`,
`restart --no-block`, and `stop --no-block` operations for that unit; it does
not grant a shell or wildcard command. Spark 1 had no installed copy of this
helper or policy.

Do not copy the live `authorized_keys` line into Git: even public-key material
and deployment fingerprints are host identity metadata. Use the installer to
create a new restricted entry only if deliberately restoring the legacy
rank-service architecture. Decommissioning the old controller is a separate,
reviewed administrative action; the Mia deployment neither invokes nor
removes it.

## Docker and model artifacts

All three ring nodes used by the documented maintenance tooling must have:

- the exact arm64 container digest recorded in `dspark_mia/UPSTREAM.lock`;
- `/dev/infiniband` available to containers; and
- the rootful NVIDIA Docker runtime.

C1 and C2 must also have a complete local model matching the selected TP2
profile's lock (the active agent service uses
`dspark_mia/MODEL.abliterated-fp8.lock.json`). The audited C3 also has that
checkpoint staged for the retained PP3 compatibility harness, but the ring
NCCL verifier itself needs only the pinned image and production TP2 never reads
C3's model copy.

The active low-concurrency Compose identity is:

```text
project: mia-dspark-agent
service: vllm-dspark
rank 0: Spark 1
rank 1: Spark 2
network: host
restart policy: no
```

The checkpoint is mounted read-only at the lock's container path. The image,
model, caches, and writable container layers are deployment artifacts, not Git
content.

The overlay declares a `500000/500000` nofile soft/hard limit. The active C8
cold generation was inspected on both ranks: Docker's configured soft/hard
values and the process-visible `ulimit` values were all 500,000, with zero
container restarts and no OOM flag. Recheck after every coordinated cold
reload; a running container never inherits a changed Compose limit
retroactively.

## OpenClaw control-host artifacts

OpenClaw is intentionally installed on a third computer, not on either Spark.
The validated headless macOS deployment adds:

| Installed path | Public source | Notes |
| --- | --- | --- |
| `/Library/LaunchDaemons/ai.openclaw.gateway.headless.plist` | rendered from `openclaw/ai.openclaw.gateway.headless.plist.in` | system-domain service; runs as the unprivileged OpenClaw user |
| `${HOME}/.openclaw/ops/install-headless-macos.sh` | `openclaw/install-headless-macos.sh` | verifier/installer copied beside private state |
| `${HOME}/.openclaw/openclaw.json` | merge `openclaw/scenefit-ds4f.patch.json` into local config | private active config; do not copy back into Git |
| `${HOME}/.openclaw/ops/slack-heartbeat/` | `openclaw/slack-heartbeat/` | guarded `@openclaw/slack` 2026.7.1 five-second liveness patcher and verifier |
| `${HOME}/.openclaw/backups/slack-thinking-heartbeat/` | no tracked equivalent | owner-only backups of locally patched Slack adapter bundles |
| `${HOME}/.openclaw/.env` | no tracked equivalent | mode 0600 provider/channel credentials |
| `${HOME}/.openclaw/workspace/AGENTS.md` | append `openclaw/AGENTS-routing.md` | local-to-Sol escalation policy |
| `${HOME}/.openclaw/workspace/news-digest/{news_digest.py,news_briefing.py,news-digest.py,digest-poster.py}` | `openclaw/news-digest/` | deterministic collector, tool-free OpenClaw prioritizer, and transactional Slack poster |
| `${HOME}/.openclaw/workspace/news-digest/.news_digest.sqlite3` | no tracked equivalent | mode 0600 pending/emitted queue, immutable delivery manifests, successful-run watermark, and selected ArXiv cache |
| `${HOME}/.openclaw/workspace/news-digest/.x_creds.json` | no tracked equivalent | optional mode 0600 X credentials; never copy into Git |

The LaunchDaemon plist contains paths and service identity but no provider
credentials. The dotenv, gateway token, sessions, audit history, and config
backup archives remain private runtime state.

## Runtime and private state

These locations intentionally have no tracked public mirror:

| State | Typical location | Reason |
| --- | --- | --- |
| supervisor persistent state | `/var/lib/dgx-spark-dspark-mia/` | generation identity/ownership |
| supervisor runtime state | `/run/dgx-spark-dspark-mia/` | ephemeral stop/lock state |
| user lifecycle locks | `${HOME}/.local/state/dgx-spark-dspark-mia-locks/` | host-local concurrency control |
| SSH private keys | `${HOME}/.ssh/` | credentials |
| Docker authentication | `${HOME}/.docker/` or root Docker config | credentials |
| Hugging Face auth/cache | `${HOME}/.cache/huggingface/` and CLI config | tokens, large artifacts |
| model weights | `MODEL_DIR` | large licensed/pinned artifact |
| Audio8 checkpoint | `${HOME}/models/Audio8-TTS-Preview-0.6b--f9612f13/` | large pinned artifact |
| approved TTS reference and transcript | operator-chosen directory outside Git | permissioned media and private conditioning data |
| dashboard live env | `/etc/default/dgx-spark-laguna-dashboard` | LAN topology and optional auth |
| dashboard TLS key | `/etc/nginx/ssl/*.key` | private key |
| OpenClaw home/state | `${HOME}/.openclaw/` | credentials, identity, sessions, history |
| logs and raw benchmark trees | repository-ignored runtime paths | prompts, responses, host details |

Never reconstruct these by copying from another person's public repository.
Create local credentials and state on each installation.

The audited account also had a site-local broad sudo rule. It is not a
required project artifact and must not be published. Fresh installations
should use the temporary bootstrap/revoke flow in [HOST_TUNING.md](HOST_TUNING.md)
and retain only explicitly accepted long-term authority.

## Drift checks

Use read-only comparisons before changing a live host:

```bash
# Choose the correct canonical role file on each host.
sudo cmp --silent netplan/cerebrus1-40-cx7.yaml /etc/netplan/40-cx7.yaml \
  && echo "Cerebrus 1 Netplan matches"

systemctl cat dgx-spark-dspark-mia.service
systemctl show dgx-spark-dspark-mia.service \
  -p LoadState -p UnitFileState -p ActiveState -p SubState -p MainPID

sudo docker ps \
  --filter label=com.docker.compose.project=mia-dspark-agent \
  --filter label=com.docker.compose.service=vllm-dspark

MIA_ENV_FILE=mia-agent.env dspark_mia/bin/probe.sh
```

On C2/C3, compare against the corresponding canonical file. For unit templates,
use the installer to render into a temporary directory and
`systemd-analyze verify` it; do not compare a parameterized `.in` file
byte-for-byte with its installed rendering.

The 2026-07-29 audit found the project unit files, direct-rail Netplan, Nginx
site, and project environment files consistent with their then-current
repository sources. Re-run checks after every pull because that statement is
a dated observation, not a permanent guarantee.

## Public-release checklist

Before committing installation material:

1. stage only intended files;
2. run `scripts/check-public.sh --staged`;
3. inspect `git diff --cached --name-only` and `git diff --cached`;
4. confirm no live `.env`, private key, TLS key, shell startup file, raw log,
   request trajectory, or model file is staged;
5. keep the upstream recipe as a pinned submodule; and
6. publish curated aggregate measurements, not private prompts or responses.
