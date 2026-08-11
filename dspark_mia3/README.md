# Three-Spark DS4F PP3 trial

This directory is a self-contained, reversible experiment for one GB10 GPU on
each of `cerberus1`, `cerberus2`, and `cerberus3`. It does not modify the live
two-node service, its Compose project, its ports, its temporary files, or its
boot supervisor. The isolated identity is:

| Item | Trial value |
| --- | --- |
| Placement | TP1 / PP3 / native `mp`, ranks 0-2 on three hosts |
| Project | `mia-dspark-pp3-trial` |
| API | `http://cerberus1.local:8893/v1` |
| Rendezvous | runtime-resolved `cerberus1` IPv4 on the management LAN, port `29633` |
| Image | immutable Anemll DSpark image digest in `UPSTREAM.lock` |
| Model | immutable abliterated FP8 checkpoint in `MODEL.lock.json` |
| KV cache | `nvfp4_ds_mla` |
| Scheduler | 1M context ceiling, C8, 8,192 batched tokens |
| Thinking | enabled |

Status: this is a retained compatibility/reproduction harness, not a working
three-rank serving configuration with the pinned runtime. The completed
model-compatibility attempt temporarily used NCCL Socket over the management
LAN; it was not a successful RoCE-ring run. Target-only PP3 formed that
three-rank world, selected the automatic `14,15,14` layer
partition, and loaded all weights. Engine initialization then failed before
API readiness because the DeepSeek V4 compressed state-cache kernel required
`state_cache.strides[0]` to be divisible by 16. Native DSpark/DFlash is
separately incompatible with pipeline parallelism. Consequently there is no
valid PP3 C1/C2/C4/C8 throughput result.

The management LAN deliberately carries rendezvous, Gloo, and TCP bootstrap.
NCCL is constrained to all four ConnectX-7 RoCE HCAs and sets
`NCCL_IB_SUBNET_AWARE_ROUTING=1` for the ring. It selects NCCL's internal
network plugin and deliberately leaves `NCCL_CROSS_NIC` at NCCL's default.
The HCA selector uses exact device names without fixed rail IDs, because each
ring edge reaches a differently named physical port on its peer.
This directory does not assign
ring IP addresses; the cluster networking recipe owns that configuration.

## Render a portable local profile

The tracked `mia3.env` preserves canonical `cerberus1-3` roles, the audited
account, checkout, model path, and active abliterated-model revision. It never
stores DHCP management addresses. Do not edit it to adapt a fresh clone. From
the repository root, render an ignored host-local profile instead:

```bash
cd "$HOME/sparks"
scripts/configure-mia3-profile.sh
export MIA3_ENV_FILE=mia3.local.env
```

The renderer defaults the remote checkout to the current checkout, the shared
identity to `~/.ssh/id_ed25519_dgx_cluster`, and the model to the active pinned
checkpoint under `~/models`. Override `MIA3_REMOTE_REPO_ROOT`,
`MIA3_CLUSTER_SSH_KEY`, `MIA3_MODEL_HOST_PATH`, `MIA3_HF_CACHE`, or
`MIA3_TMP_HOST` before running it when the site differs. Use
`MIA3_PROFILE_NAME` for another safe `.env` basename and `--force` only when
deliberately replacing that generated file.

`MIA3_ENV_FILE` may be the basename or absolute path of a regular profile
directly inside `dspark_mia3/`. Export it once, as above, and use the same
selector for `sync.sh`, `sync-model.sh`, `preflight.sh`, `start.sh`,
`status.sh`, `logs.sh`, and `stop.sh`. `sync.sh` copies the selected profile to
both remote trial directories, and remote lifecycle calls select that same
basename. Falling back to the tracked `mia3.env` after provisioning a local
profile can silently select the audited paths, so treat the exported selector
as part of the trial identity.

## Required SSH aliases

Every host should have the same mode-0600 private key and the same host aliases
in `~/.ssh/config`. Canonical Avahi names are restricted to the management
interface and avoid pinning a DHCP lease:

```sshconfig
Host cerberus1
  HostName cerberus1.local
  User catid
  IdentityFile ~/.ssh/id_ed25519_dgx_cluster
  IdentitiesOnly yes
  IdentityAgent none

Host cerberus2
  HostName cerberus2.local
  User catid
  IdentityFile ~/.ssh/id_ed25519_dgx_cluster
  IdentitiesOnly yes
  IdentityAgent none

Host cerberus3
  HostName cerberus3.local
  User catid
  IdentityFile ~/.ssh/id_ed25519_dgx_cluster
  IdentitiesOnly yes
  IdentityAgent none
```

Runtime resolution tries canonical `cerberusN.local` first, then canonical
`cerberusN.lan`. Only if both are absent does it try the explicit
transitional `cerebrusN` and `sparkN` local-domain aliases. A result is accepted
only when it is owned by local `enP7s7` for that rank or routes through
`enP7s7` with a source address owned by that interface. Ambiguous DNS, stale
local DNS, and routes through Wi-Fi or another interface fail closed. Numeric
addresses are passed only into the Compose/vLLM process environment.

Each public key must appear once in `authorized_keys` on all three hosts. Never
commit the private key or credentials to this repository.

## Why this is PP3, not TP3

The checkpoint has 64 attention heads and 256 routed experts. Neither is
divisible by three, so TP3 is rejected before Docker is touched. With one GPU
per host, TP1/PP3 is the only three-rank decomposition attempted here. It is
not currently viable: target-only PP3 reaches model initialization but fails
the compressed state-cache stride requirement, while the native DSpark
proposer rejects pipeline parallelism earlier. Target-only remains the default
solely to preserve the closest reproduction path; speculative mode is an
explicit negative compatibility test.

There are four layer-partition profiles. `default` leaves
`VLLM_PP_LAYER_PARTITION` unset inside the container; the explicit profiles
all sum to the checkpoint's 43 transformer layers:

```bash
MIA3_PARTITION_PROFILE=default ./bin/validate-static.sh
MIA3_PARTITION_PROFILE=14-15-14 ./bin/validate-static.sh
MIA3_PARTITION_PROFILE=15-15-13 ./bin/validate-static.sh
MIA3_PARTITION_PROFILE=16-15-12 ./bin/validate-static.sh
```

On the pinned runtime, automatic 43/3 partitioning resolves to `14,15,14`;
the two other explicit choices move work away from the final stage to test the
effect of its output norm and language-model head.

## Provisioning artifacts

Run lifecycle commands from `cerberus1`. Code sync is small and is performed
automatically by `start.sh`; it replaces files only inside the dedicated
remote `dspark_mia3` directories. Model sync is deliberately separate because
it transfers roughly 166 GB and never deletes remote files:

```bash
export MIA3_ENV_FILE=mia3.local.env
./bin/sync.sh
./bin/sync-model.sh 2       # new cerberus3 only
./bin/preflight.sh          # read-only checks on all ranks
```

Preflight requires, on every host: exact synchronized integration files, all
four 200 Gb/s functions at MTU 9000 with IPv4 addresses, four active RoCE
links, authoritative management-name resolution on `enP7s7`, the pinned local
image, the complete exact model,
free trial ports, and no other active vLLM process. It never pulls an image,
downloads a model, or stops production.

## Lifecycle

The commands below reproduce the isolated trial and its current failure. Do
not run them expecting an API on port 8893 unless a newer runtime explicitly
adds DeepSeek V4 pipeline-parallel support and passes the complete preflight,
startup, and response validation.

Launch is worker-first (`rank2`, `rank1`, then API `rank0`) and rolls back only
the trial project if any rank exits or API readiness times out:

```bash
export MIA3_ENV_FILE=mia3.local.env

# Target-only, automatic partition
./bin/start.sh

# Target-only, explicit partition
MIA3_PARTITION_PROFILE=15-15-13 ./bin/start.sh

# Native DSpark (called DFlash by this trial selector) compatibility trial;
# the pinned image deterministically rejects DSpark with pipeline parallelism
MIA3_PARTITION_PROFILE=15-15-13 MIA3_DFLASH=on ./bin/start.sh

MIA3_PARTITION_PROFILE=15-15-13 ./bin/status.sh
MIA3_PARTITION_PROFILE=15-15-13 ./bin/logs.sh 2 300
MIA3_PARTITION_PROFILE=15-15-13 ./bin/stop.sh
```

Use the same profile, partition, and DFlash selectors for every command in a
generation. Compose uses
`restart: "no"`; a rank cannot independently rejoin an existing collective.
There is intentionally no systemd unit for this benchmark trial.

`status.sh` always reports rank 0 locally before attempting either worker, and
`stop.sh` starts rank-0 cleanup independently. Their `ps`/`down` Compose
renders use documentation-only interpolation addresses, so broken peer DNS or
SSH cannot hide or strand the local trial container. Every command that can
launch a rank still requires the strict `enP7s7` hostname/address validation.

## Tests

```bash
./tests/run.sh
```

The tests syntax-check every shell script, validate all layer profiles, inspect
the Compose source, render every rank when rootful Compose is available, scan
for credential-like strings, and prove that TP3 fails with a model-specific
divisibility explanation.
