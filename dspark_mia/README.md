# Pinned Mia DSpark integration

This is an isolated two-rank integration of the MiaAI-Lab recipe on
`cerebrus1` and `cerebrus2`. The hosts are part of a three-Spark physical
ring, but `cerebrus3` is not a vLLM rank. The
upstream checkout is detached at commit
`0220360b752349c9b3129d64799246a4ec106640` and remains unmodified. All local
changes live beside it in this directory.

## Controlled seq6 profile

| Setting | Pinned value |
| --- | --- |
| Upstream | `MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark@0220360b752349c9b3129d64799246a4ec106640` |
| Image | `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8` |
| Model | `deepseek-ai/DeepSeek-V4-Flash-DSpark@62af8fffb2f7030cac4de2f0169f5b8d1101b646` |
| Model path on each host | `$HOME/models/DeepSeek-V4-Flash-DSpark-official` by default |
| Parallelism | TP=2, PP=1, one rank per Spark |
| Speculation | DSpark, probabilistic, `k=5` |
| Context / slots | 1,048,576 / 6; 8,192 max batched tokens |
| KV / memory | `nvfp4_ds_mla`, GPU memory utilization 0.80 |
| Chat template | `thinking=true` |
| API / rendezvous | `0.0.0.0:8888` / `10.10.84.28:29630` |
| Compose project | `mia-dspark-pinned` |
| SSH identity | `$HOME/.ssh/id_ed25519_dgx_cluster` by default, with `IdentitiesOnly=yes` |

`MODEL.lock.json` records the default path hint, HF revision, checkpoint index
and config hashes, 48-shard count, and total Safetensors bytes. The selected
profile supplies the actual host path and may select another lock with
`MIA_MODEL_LOCK=NAME.json`. A selected lock must be a regular, non-symlink
JSON file directly inside this directory. Serving is offline and the
checkpoint is mounted read-only. `scripts/download-pinned-model.sh` honors the
selected profile and lock; lifecycle commands never download model data.

Static rendering does not pretend that artifacts exist. `bin/preflight.sh`
separately requires the exact image digest and complete pinned checkpoint on
both nodes before any launch.

The tracked profiles preserve the audited installation. New users should
render an ignored profile with local paths, then keep the same selector on
every provisioning and lifecycle command:

```bash
../scripts/configure-dspark-profile.sh --model active
MIA_ENV_FILE=mia-throughput.local.env ./bin/validate-static.sh

../scripts/configure-dspark-profile.sh --profile agent --model active
MIA_ENV_FILE=mia-agent.local.env ./bin/validate-static.sh
```

## Throughput benchmark profile

`mia-throughput.env` keeps the same 1M context ceiling, thinking mode, DSpark
k5, pinned artifacts, and the direct TP2 edge while isolating lifecycle identity:

| Setting | Throughput value |
| --- | --- |
| Compose project | `mia-dspark-throughput` |
| Served model name | `deepseek-v4-flash-dspark-mia-throughput` |
| API / rendezvous | `0.0.0.0:8889` / `10.10.84.28:29631` |
| Host tmp | `$HOME/.cache/dspark-mia-throughput-tmp` |
| Context / slots | 1,048,576 / 32 |
| Max batched tokens | 8,192 |
| CUDA-graph capture | 192 = `32 * (k5 + target slot)` |
| GPU memory utilization | 0.78 |

The 8,192 scheduler budget is deliberately conservative. Live k5 evidence
shows vLLM reserves `max_num_seqs * (k-1)` draft slots; at C32 that leaves
8,064 scheduled tokens. Chunked prefill admits 32 rendered inputs of
approximately 1,024 tokens in five bounded chunks instead of stacking a 32K
activation peak on the larger graph capture. The graph ceiling is
192 = `32 * (k+1)`; the pinned runtime retained it and captured the complete
target and DSpark graph sets through C32. GPU utilization stays at 0.78 to
give capture more headroom.

## Low-concurrency agent profile

`mia-agent.env.example` is the OpenClaw-oriented alternative to the C32
throughput profile. It retains the default pinned model/image, TP2/PP1
placement, native DSpark k5 proposer, one-million-token per-request ceiling,
thinking mode, direct-edge transport, API port, and public model ID. The tracked
installed `mia-agent.env` currently selects the separately pinned abliterated
FP8 lock while retaining those client-facing IDs. Its isolated
Compose/rendezvous/tmp identity and reduced scheduler are:

| Setting | Agent value |
| --- | --- |
| Compose project | `mia-dspark-agent` |
| Served model names | historical ID plus canonical `deepseek-v4-flash` alias |
| API / rendezvous | `0.0.0.0:8889` / `10.10.84.28:29632` |
| Host tmp | `$HOME/.cache/dspark-mia-agent-tmp` |
| Context / slots | 1,048,576 / 8 |
| Max batched tokens | 8,192 |
| CUDA-graph capture | 48 = `8 * (k5 + target slot)` |
| GPU memory utilization | 0.78 |

The fixed 1,024-in/1,024-out C1-C8 comparison found speed effectively neutral
versus C32, while captured graph memory fell from 2.59 GiB to 1.27 GiB. This
profile removes the unused C9-C32 graph set and prevents more than eight active
requests from expanding the decode batch; additional requests wait in vLLM's
queue. The 8,192-token scheduler budget remains deliberate for long chunked
prefills. Lowering it can improve decode fairness while another request
prefills, but can also increase time to first token on a long agent transcript
and must be measured. See
[`../results/DEEPSEEK_V4_C8_AGENT_PROFILE.md`](../results/DEEPSEEK_V4_C8_AGENT_PROFILE.md).

Render a separate ignored profile so selection and rollback stay explicit:

```bash
../scripts/configure-dspark-profile.sh --profile agent --model active
MIA_ENV_FILE=mia-agent.local.env ./bin/validate-static.sh
```

Switching the boot supervisor to this profile requires a coordinated
`restart`; `start` may adopt a healthy existing generation and therefore does
not apply new scheduler limits to already-running containers.

## Rank-specific direct-edge transport

The ranks face one another on different physical port numbers:

```text
HEAD_NCCL_IB_HCA='=rocep1s0f1:1:0,roceP2p1s0f1:1:0'
WORKER_NCCL_IB_HCA='=rocep1s0f0:1:0,roceP2p1s0f0:1:0'
NCCL_NETDEVS_POLICY=ALL
NCCL_CROSS_NIC=0
NCCL_IB_MERGE_NICS=0
NCCL_SOCKET_IFNAME='=enP7s7'
TP_SOCKET_IFNAME=enP7s7
GLOO_SOCKET_IFNAME=enP7s7
```

There is deliberately no `NCCL_IB_GID_INDEX`. The compose override removes the
upstream scalar, the wrapper unsets any inherited value, injects the facing
HCA expression separately for each rank, and static validation checks both
rendered environments. Before launch, preflight uses readiness `--scope tp2`
on C1 and C2 to require carrier, MTU 9000, exact `192.168.0/1` addresses,
active RDMA, and peer reachability on both logical links. It deliberately does
not depend on C3.

Do not change this service to TP3. The model has 64 attention heads and 256
routed experts; neither is divisible by three. Target-only PP3 reached weight
load but failed the DeepSeek V4 compressed state-cache stride requirement
during engine initialization. Native DSpark/DFlash separately lacks the
pipeline-parallel protocol. Three-node ring experiments therefore belong in a
separate launcher/profile and are not a working serving topology.

## Validation

Static validation neither pulls nor starts anything:

```bash
MIA_ENV_FILE=mia-agent.local.env ./bin/validate-static.sh
MIA_ENV_FILE=mia-throughput.env ./tests/test-profile-selection.sh
./tests/test-profile-renderer.sh
./tests/test-model-catalog.sh
MIA_ENV_FILE=mia-throughput.env ./tests/test-start-timeout.sh
```

Generated local profiles are the fresh-clone path. The tracked `mia.env`,
`mia-throughput.env`, and `mia-agent.env` files preserve this audited
deployment and contain its original absolute paths.

The timeout test uses fixture helpers and fake `ssh`/`curl`; it never contacts
Docker or C2. It proves that a failed API wait tears down both isolated
ranks rather than leaving the headless worker behind.

An explicitly selected profile must be a regular `.env` file directly inside
this integration root. `common.sh` canonicalizes and exports the choice. The
worker receives the exact matching basename for remote validation, compose,
status, and rollback; it never silently falls back to `mia.env`. Sync also
compares the selected profile's SHA-256 on C1 and C2.

All Docker and Compose operations run as `sudo -n docker`. Rank, headless, and
fabric-IP values are supplied through an explicit root-side `env` invocation;
the wrappers do not depend on sudo preserving the caller's ambient variables.
If passwordless Docker authority is unavailable, validation/preflight fails
instead of falling back to a user socket.

The full preflight is also non-mutating. It additionally checks the direct TP2
edge, free ports, absence of an active vLLM workload, exact local
image availability, and complete pinned model trees:

```bash
MIA_ENV_FILE=mia-agent.env ./bin/sync-worker.sh
MIA_ENV_FILE=mia-agent.env ./bin/preflight.sh

# C32 alternative:
MIA_ENV_FILE=mia-throughput.env ./bin/sync-worker.sh
MIA_ENV_FILE=mia-throughput.env ./bin/preflight.sh
```

The image and model checks are intentionally local-only. If either artifact is
missing, preflight reports it; it does not pull or download it.

## Transient launch

This profile is port-isolated from the existing port-8000 deployment, but two
120B services cannot safely share the same GPU memory. The launcher refuses to
continue while any vLLM workload is active and never stops or changes the
existing services. Stop those workloads explicitly before a controlled trial.

The bare commands below select the legacy `mia.env` profile:

```bash
./bin/start.sh
./bin/status.sh
```

For the throughput project, keep the same selector on every lifecycle command:

```bash
MIA_ENV_FILE=mia-throughput.env ./bin/start.sh
MIA_ENV_FILE=mia-throughput.env ./bin/status.sh
```

The launcher syncs this pinned tree to the same path on C2, revalidates
both copies, and starts rank 1 headless before rank 0. The legacy `mia.env`
profile waits on `http://127.0.0.1:8888/v1/models`; the C8 agent and C32
throughput profiles both wait on port 8889. Compose has `pull_policy: never`,
`restart: "no"`, and a unique project name. The lack of per-container restart
is intentional: one TP rank cannot safely restart and rejoin the other rank's
existing NCCL collective. The optional C1 supervisor below always
recycles the complete two-rank generation.

The local overlay also requests a `500000/500000` soft/hard `nofile` limit.
The active C8 generation passed both Docker and process-visible checks for that
limit on both ranks. Existing containers still retain their creation-time
limit, so recheck after every coordinated pair recreation.

## Optional boot persistence

The Compose projects remain non-restarting and scoped. For either selected
profile, C1 can own boot orchestration through
`systemd/dgx-spark-dspark-mia.service`. The long-running supervisor adopts an
already healthy generation without interrupting it, or launches rank 1 first
and rank 0 second if recovery is needed.

Every poll verifies:

- exactly one running project-labelled container on each Spark;
- each container's ID, start timestamp, OOM state, and host boot ID;
- the rank-0 `/health` response and every required model ID advertised by
  `/v1/models`, including the canonical OpenClaw alias.

A missing, stopped, OOM-killed, independently restarted, or replaced rank
causes an immediate coordinated stop/start of both ranks. API and management
SSH failures use consecutive-failure thresholds to tolerate short stalls; an
SSH-only outage is given a longer grace period while the model API is still
healthy. Probes, Docker cleanup, and cold starts all have hard wall-clock
limits, so a wedged Docker daemon cannot wedge the supervisor indefinitely.
Failed cold starts retry forever with bounded exponential backoff. C2 must not
run an autonomous copy of this unit; C1 is the sole orchestrator
and Compose continues to use `restart: "no"`.

Install and enable the unit only after disabling the retired rank-0 service;
do not enable both:

```bash
MIA_ENV_FILE=mia-agent.local.env \
  ../scripts/install-dspark-supervisor.sh enable
```

Starting the unit over an existing healthy selected project is
non-disruptive. Confirm that systemd retains a live main process and recorded
generation:

```bash
sudo systemctl start dgx-spark-dspark-mia.service
systemctl show dgx-spark-dspark-mia.service \
  -p ActiveState -p SubState -p MainPID
sudo cat /var/lib/dgx-spark-dspark-mia/epoch
MIA_ENV_FILE=mia-agent.env ./bin/probe.sh
```

`ActiveState=active`, `SubState=running`, and a nonzero `MainPID` distinguish
the supervisor from the retired oneshot launcher. Because this is a
long-running `Type=simple` monitor, systemd activity alone does not mean the
model has finished its several-minute initialization; `/health` and
`bin/probe.sh` are the readiness authorities. Stopping or disabling the unit
removes both ranks that it owns and clears its generation epoch. If systemd
invokes cleanup before a supervisor has claimed ownership,
`stop-if-owned.sh` leaves an existing manually launched generation alone.
Direct `start.sh` and `stop.sh` calls are rejected while the supervisor holds
the shared lifecycle lock.

For a no-reboot recovery test, first resolve the exact container through both
Compose labels, record both fingerprints, and kill only that verified
container. Never kill by a broad image or process match. The supervisor should
replace both IDs and timestamps and return the same model API:

```bash
sudo docker ps \
  --filter label=com.docker.compose.project=mia-dspark-agent \
  --filter label=com.docker.compose.service=vllm-dspark
MIA_ENV_FILE=mia-agent.env ./bin/probe.sh
```

Cold initialization includes CUDA-graph capture and can take several minutes.
Follow it with `journalctl -fu dgx-spark-dspark-mia.service`; do not start a
second launcher while the supervisor lock is held.

## Profile switching and rollback

With the supervisor enabled, switch profiles through the installer so it
re-renders the unit and replaces both TP ranks as one generation:

```bash
# C8 agent -> C32 throughput
MIA_ENV_FILE=mia-throughput.local.env \
  ../scripts/install-dspark-supervisor.sh restart

# C32 throughput -> C8 agent
MIA_ENV_FILE=mia-agent.local.env \
  ../scripts/install-dspark-supervisor.sh restart
```

Do not call `bin/stop.sh` while the supervisor is active; the lifecycle lock
rejects it. To stop serving completely, stop the unit and let its owned-pair
cleanup run:

```bash
sudo systemctl stop dgx-spark-dspark-mia.service
```

For a manually launched generation with the supervisor inactive, stop only
the selected isolated project:

```bash
MIA_ENV_FILE=mia-agent.env ./bin/stop.sh
# Or: MIA_ENV_FILE=mia-throughput.env ./bin/stop.sh
```

Rollback does not delete the pinned source, model, or image and does not
modify the retired port-8000 units.

## Layout

- `upstream/`: pristine detached Git checkout at the locked commit
- `UPSTREAM.lock`: source tree, image digest, and model revision provenance
- `MODEL.lock.json`: location-independent checkpoint metadata and path hint
- `MODEL.abliterated-fp8.lock.json`: alternate agent-profile checkpoint pin
- `mia.env`: pinned cluster and runtime values
- `mia-agent.env`: audited C8 / 1M-context agent profile for this checkout
- `mia-agent.env.example`: portable input for `--profile agent`
- `mia-throughput.env`: isolated seq32 / conservative 8,192-token benchmark profile
- `mia-throughput.env.example`: portable input to the profile renderer
- `compose.mia.override.yml`: rank-specific direct-edge/thinking/image/model override
- `bin/`: validation, readiness, lifecycle wrappers, health probe, and supervisor
- `tests/`: profile/lock propagation, rollback, and deterministic supervisor tests
