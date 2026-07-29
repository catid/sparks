# Pinned Mia DSpark integration

This is an isolated two-Spark integration of the MiaAI-Lab recipe. The
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
| API / rendezvous | `0.0.0.0:8888` / `192.168.100.10:29630` |
| Compose project | `mia-dspark-pinned` |
| SSH identity | `$HOME/.ssh/id_ed25519_dgx_cluster` by default, with `IdentitiesOnly=yes` |

`MODEL.lock.json` records the default path hint, HF revision, checkpoint index
and config hashes, 48-shard count, and total Safetensors bytes. The selected
profile supplies the actual host path. Serving is offline and the checkpoint
is mounted read-only. `scripts/download-pinned-model.sh` performs an explicit
pinned download; lifecycle commands never download model data.

Static rendering does not pretend that artifacts exist. `bin/preflight.sh`
separately requires the exact image digest and complete pinned checkpoint on
both nodes before any launch.

The tracked profiles preserve the audited installation. New users should
render an ignored profile with local paths, then keep the same selector on
every provisioning and lifecycle command:

```bash
../scripts/configure-dspark-profile.sh
MIA_ENV_FILE=mia-throughput.local.env ./bin/validate-static.sh
```

## Throughput benchmark profile

`mia-throughput.env` keeps the same 1M context ceiling, thinking mode, DSpark
k5, pinned artifacts, and four rails while isolating all lifecycle identity:

| Setting | Throughput value |
| --- | --- |
| Compose project | `mia-dspark-throughput` |
| Served model name | `deepseek-v4-flash-dspark-mia-throughput` |
| API / rendezvous | `0.0.0.0:8889` / `192.168.100.10:29631` |
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

## Four-rail transport

The local override uses the already proven topology:

```text
=rocep1s0f0:1:0,roceP2p1s0f0:1:0,rocep1s0f1:1:1,roceP2p1s0f1:1:1
NCCL_NETDEVS_POLICY=ALL
NCCL_CROSS_NIC=0
NCCL_IB_MERGE_NICS=0
```

There is deliberately no `NCCL_IB_GID_INDEX`. The compose override removes the
upstream scalar, the wrapper unsets any inherited value, and static validation
checks the rendered environment. Before launch, preflight reuses
`../bin/wait-cx7-ready.sh --check-once` on both Sparks to require carrier,
MTU 9000, the expected 192.168.100-103 addresses, active RDMA links, and
peer reachability on all four rails.

## Validation

Static validation neither pulls nor starts anything:

```bash
export MIA_ENV_FILE=mia-throughput.local.env
./bin/validate-static.sh
./tests/test-profile-selection.sh
./tests/test-start-timeout.sh
```

The generated local profile is the fresh-clone path. The tracked `mia.env` and
`mia-throughput.env` files preserve the audited deployment and contain its
original absolute paths.

The timeout test uses fixture helpers and fake `ssh`/`curl`; it never contacts
Docker or Spark 2. It proves that a failed API wait tears down both isolated
ranks rather than leaving the headless worker behind.

An explicitly selected profile must be a regular `.env` file directly inside
this integration root. `common.sh` canonicalizes and exports the choice. The
worker receives the exact matching basename for remote validation, compose,
status, and rollback; it never silently falls back to `mia.env`. Sync also
compares the selected profile's SHA-256 on Spark 1 and Spark 2.

All Docker and Compose operations run as `sudo -n docker`. Rank, headless, and
fabric-IP values are supplied through an explicit root-side `env` invocation;
the wrappers do not depend on sudo preserving the caller's ambient variables.
If passwordless Docker authority is unavailable, validation/preflight fails
instead of falling back to a user socket.

The full readiness check is also non-mutating. It additionally checks both
hosts' four rails, free ports, absence of an active vLLM workload, exact local
image availability, and complete pinned model trees:

```bash
./bin/sync-worker.sh
./bin/preflight.sh

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

When ready:

```bash
./bin/start.sh
./bin/status.sh
```

For the throughput project, keep the same selector on every lifecycle command:

```bash
MIA_ENV_FILE=mia-throughput.env ./bin/start.sh
MIA_ENV_FILE=mia-throughput.env ./bin/status.sh
```

The launcher syncs this pinned tree to the same path on Spark 2, revalidates
both copies, starts rank 1 headless first, then rank 0, and waits for
`http://127.0.0.1:8888/v1/models`. Compose has `pull_policy: never`,
`restart: "no"`, and a unique project name. The lack of per-container restart
is intentional: one TP rank cannot safely restart and rejoin the other rank's
existing NCCL collective. The optional Spark 1 supervisor below always
recycles the complete two-rank generation.
The throughput profile waits on `http://127.0.0.1:8889/v1/models`.

The local overlay also requests a `500000/500000` soft/hard `nofile` limit.
Existing containers retain their creation-time limit; this applies on the
next coordinated pair recreation.

## Optional boot persistence

The Compose projects remain non-restarting and scoped. For the selected
throughput deployment, Spark 1 can own boot orchestration through
`systemd/dgx-spark-dspark-mia.service`. The long-running supervisor adopts an
already healthy generation without interrupting it, or launches rank 1 first
and rank 0 second if recovery is needed.

Every poll verifies:

- exactly one running project-labelled container on each Spark;
- each container's ID, start timestamp, OOM state, and host boot ID;
- the rank-0 `/health` response and exact model advertised by `/v1/models`.

A missing, stopped, OOM-killed, independently restarted, or replaced rank
causes an immediate coordinated stop/start of both ranks. API and management
SSH failures use consecutive-failure thresholds to tolerate short stalls; an
SSH-only outage is given a longer grace period while the model API is still
healthy. Probes, Docker cleanup, and cold starts all have hard wall-clock
limits, so a wedged Docker daemon cannot wedge the supervisor indefinitely.
Failed cold starts retry forever with bounded exponential backoff. Spark 2
must not run an autonomous copy of this unit; Spark 1 is the sole orchestrator
and Compose continues to use `restart: "no"`.

Install and enable the unit only after disabling the retired rank-0 service;
do not enable both:

```bash
MIA_ENV_FILE=mia-throughput.local.env \
  ../scripts/install-dspark-supervisor.sh enable
```

Starting the unit over an existing healthy throughput project is
non-disruptive. Confirm that systemd retains a live main process and recorded
generation:

```bash
sudo systemctl start dgx-spark-dspark-mia.service
systemctl show dgx-spark-dspark-mia.service \
  -p ActiveState -p SubState -p MainPID
sudo cat /var/lib/dgx-spark-dspark-mia/epoch
MIA_ENV_FILE=mia-throughput.env ./bin/probe.sh
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
  --filter label=com.docker.compose.project=mia-dspark-throughput \
  --filter label=com.docker.compose.service=vllm-dspark
MIA_ENV_FILE=mia-throughput.env ./bin/probe.sh
```

Cold initialization includes CUDA-graph capture and can take several minutes.
Follow it with `journalctl -fu dgx-spark-dspark-mia.service`; do not start a
second launcher while the supervisor lock is held.

## Rollback

Rollback only the isolated Compose project:

```bash
./bin/stop.sh
sudo -n docker ps -a --filter label=com.docker.compose.project=mia-dspark-pinned
ssh -i "$HOME/.ssh/id_ed25519_dgx_cluster" -o IdentitiesOnly=yes spark2 \
  sudo -n docker ps -a --filter label=com.docker.compose.project=mia-dspark-pinned
```

Throughput rollback is equally scoped and must retain its selector:

```bash
MIA_ENV_FILE=mia-throughput.env ./bin/stop.sh
sudo -n docker ps -a --filter label=com.docker.compose.project=mia-dspark-throughput
```

Both container listings should be empty. The rollback does not delete the
pinned source/model/image and does not start, stop, enable, disable, or edit
the existing port-8000 units. Those services can be resumed through their
existing runbook after this project is confirmed down.

## Layout

- `upstream/`: pristine detached Git checkout at the locked commit
- `UPSTREAM.lock`: source tree, image digest, and model revision provenance
- `MODEL.lock.json`: location-independent checkpoint metadata and path hint
- `mia.env`: pinned cluster and runtime values
- `mia-throughput.env`: isolated seq32 / conservative 8,192-token benchmark profile
- `mia-throughput.env.example`: portable input to the profile renderer
- `compose.mia.override.yml`: local four-rail/thinking/image/model override
- `bin/`: validation, readiness, lifecycle wrappers, health probe, and supervisor
- `tests/`: profile propagation, rollback, and deterministic supervisor tests
