# Fresh three-Spark fabric and two-rank inference setup

This runbook reproduces the selected DeepSeek V4 Flash DSpark deployment on
two inference ranks over a three-DGX-Spark ring. It assumes:

- all three machines run a current DGX OS image and use their supplied 240 W NVIDIA
  power adapters;
- each machine is reachable at its documented management address; section 2
  installs the canonical `cerebrus1` through `cerebrus3` identities and local
  name map;
- the same unprivileged service user exists on all three;
- that user's home and checkout paths contain no whitespace or shell
  metacharacters (the renderers reject ambiguous service paths);
- three ConnectX-7 cables use the audited service ring: C1-P1/C2-P0,
  C1-P0/C3-P0, and C2-P1/C3-P1. This is sufficient for the independent C3
  workload role; the unsupported three-rank NCCL experiment requires the
  explicit crossed C3 variant documented in `NETWORKING.md`;
- each host has at least 400 GB free for the 166.9 GB checkpoint, an 18.8 GB
  container image, caches, and working headroom;
- the Hugging Face account used for provisioning can access the checkpoint
  selected by `--model` (active abliterated revision by default, or the
  original `deepseek-ai/DeepSeek-V4-Flash-DSpark` reference).

Commands labeled **both ranks** run on C1 and C2. Ring Netplan and fabric
validation commands run on all three nodes. Commands labeled **C1** coordinate
the TP2 pair; C3 is not a vLLM rank.

## 1. Clone and temporarily enable passwordless sudo

**All three ring nodes:**

```bash
git clone --recurse-submodules https://github.com/catid/sparks.git ~/sparks
cd ~/sparks
scripts/bootstrap-sudo.sh enable
```

The first command requiring `sudo` asks for the administrator password. The
script then installs the temporary policy
`/etc/sudoers.d/90-sparks-bootstrap-nopasswd`. This gives the service user
unrestricted root authority so an automation agent can complete the bootstrap
without password prompts.

Treat this as an attended installation window. Do not expose an agent or SSH
account with this authority to untrusted users. Section 13 replaces it with a
narrower policy and removes it.

Never copy an existing `.bashrc`, API-key file, Hugging Face token, SSH private
key, Docker auth file, OpenClaw state directory, or TLS private key into this
checkout. This is a public repository.

## 2. Assign canonical host identities

Run the role-specific dry run and apply over the independent management
connection. The installer binds each role to its exact management address,
backs up `/etc/hosts`, sets the short hostname, installs all canonical and
transitional aliases, and rolls back both files if validation fails:

```bash
# On the host at 10.10.84.28:
scripts/install-cluster-host-identity.sh cerebrus1
scripts/install-cluster-host-identity.sh cerebrus1 --apply

# On the host at 10.10.84.12:
scripts/install-cluster-host-identity.sh cerebrus2
scripts/install-cluster-host-identity.sh cerebrus2 --apply

# On the host at 10.10.84.121:
scripts/install-cluster-host-identity.sh cerebrus3
scripts/install-cluster-host-identity.sh cerebrus3 --apply
```

Open a new shell after the hostname change, then verify on every node:

```bash
hostname -s
getent ahostsv4 cerebrus1 cerebrus2 cerebrus3
```

The checked-in `hosts/cerebrus*.hosts` files intentionally publish this
non-routable reference topology. Adapt and review those sources before using
the runbook on a different management subnet.

## 3. Verify the DGX base and install host tools

The playbook builds on DGX OS; it does not replace the vendor kernel, firmware,
NVIDIA driver, CUDA toolkit, Docker Engine, Compose plugin, or NVIDIA Container
Toolkit.

**All three ring nodes:**

```bash
cd ~/sparks
scripts/install-host-packages.sh --install

uname -r
nvidia-smi
nvcc --version
sudo docker version
sudo docker compose version
```

The validated reference hosts used DGX OS 7.5.0 / Ubuntu 24.04.4, kernel
`6.17.0-1029-nvidia`, driver `580.173.02`, CUDA 13.0, Docker 29.2.1, and
Compose 5.0.2. Exact versions are recorded in
[`SOFTWARE.md`](SOFTWARE.md). Newer DGX OS releases should be tested rather
than downgraded merely to match the snapshot.

Apply DGX firmware/OS updates before downloading the model, then reboot and
confirm that no capsule update remains staged. Do not interrupt a firmware
update. The original pair received SoC/GPU and USB-C power-delivery firmware
before its final validation.

## 4. Run headlessly

**All three ring nodes:**

```bash
cd ~/sparks
scripts/configure-headless.sh enable
scripts/configure-headless.sh status
```

This changes the boot default to `multi-user.target` and stops the display
manager. It deliberately leaves GDM/X packages and NVIDIA's stock
`/etc/X11/xorg.conf` installed. To restore the desktop later:

```bash
scripts/configure-headless.sh restore-gui
```

No custom GPU clock or power limit is part of this setup. Do not add one
unless a measured workload and the hardware's supported controls justify it.
See [`HOST_TUNING.md`](HOST_TUNING.md).

## 5. Configure and validate the ConnectX-7 ring

The exact six-subnet matrix and port mapping are in
[`NETWORKING.md`](NETWORKING.md). Dry-run each canonical file first:

```bash
cd ~/sparks
scripts/install-cx7-netplan.sh cerebrus1
ssh cerebrus2 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus2'
ssh cerebrus3 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus3 --c3-port-map c3-p0-to-c1'
```

Retain console or management-network access, then apply on all three nodes:

```bash
scripts/install-cx7-netplan.sh cerebrus1 --apply
ssh cerebrus2 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus2 --apply'
ssh cerebrus3 'cd ~/sparks && scripts/install-cx7-netplan.sh cerebrus3 --c3-port-map c3-p0-to-c1 --apply'
```

Validate the whole ring with `--scope ring`. Production preflight and systemd
use `--scope tp2`, which checks only C1-P1 to C2-P0 and cannot be blocked by
C3:

```bash
CX7_NODE_ROLE=cerebrus1 bin/wait-cx7-ready.sh --check-once --scope ring
CX7_NODE_ROLE=cerebrus1 bin/wait-cx7-ready.sh --check-once --scope tp2
ssh cerebrus3 'CX7_NODE_ROLE=cerebrus3 ~/sparks/bin/wait-cx7-ready.sh --check-once --scope ring --c3-port-map c3-p0-to-c1'
rdma link show
```

## 6. Install the shared three-node cluster identity

The systemd supervisor has no interactive SSH agent. All three nodes use the
same dedicated on-disk cluster key so any Spark can reach either peer without
a laptop or forwarded agent. This intentionally increases the key's blast
radius; do not reuse a personal identity.

**C1:**

```bash
install -d -m 0700 ~/.ssh
ssh-keygen -t ed25519 \
  -f ~/.ssh/id_ed25519_dgx_cluster \
  -C dspark-cluster
chmod 0600 ~/.ssh/id_ed25519_dgx_cluster

# Bootstrap authorization through the already authenticated management path.
ssh-copy-id -i ~/.ssh/id_ed25519_dgx_cluster.pub cerebrus1
ssh-copy-id -i ~/.ssh/id_ed25519_dgx_cluster.pub cerebrus2
ssh-copy-id -i ~/.ssh/id_ed25519_dgx_cluster.pub cerebrus3

# Build a cluster-only known-hosts file. Compare every displayed fingerprint
# with `sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on that host's
# already authenticated console/session before installing this file.
cluster_scan="$(mktemp)"
for host in cerebrus1 cerebrus2 cerebrus3; do
  ssh-keyscan -H -t ed25519 "$host" >>"${cluster_scan}"
done
ssh-keygen -lf "${cluster_scan}"
install -m 0600 "${cluster_scan}" ~/.ssh/dgx_cluster_known_hosts
rm -f -- "${cluster_scan}"

# The installer copies only the dedicated keypair and verified cluster
# known-hosts file; it never copies a general ~/.ssh/config.
scripts/install-shared-cluster-key.sh --install \
  cerebrus1 cerebrus2 cerebrus3
scripts/install-shared-cluster-key.sh --verify \
  cerebrus1 cerebrus2 cerebrus3
```

`ssh/cluster.config.example` is an optional, cluster-only alias block for
interactive SSH. Review its user/path values and include a copied fragment
from `~/.ssh/config`; do not replace or distribute a workstation's complete
SSH configuration. Lifecycle scripts already pass their key options
explicitly and rely on the installed `/etc/hosts` aliases.

Use an empty key passphrase only when unattended boot recovery is required,
and compensate with a dedicated account, trusted management network, and
tightly controlled file permissions. The installer defaults to strict host-key
checking and never copies or overwrites a peer's general `~/.ssh/config`.
Use `CLUSTER_STRICT_HOST_KEY_CHECKING=accept-new` only during an attended first
contact after comparing fingerprints through an independent channel.

Rotate the shared identity as one maintenance operation: stop the supervisor,
generate a new cluster-only key in a temporary mode-0700 directory, add its
public key to all three nodes through the still-working old identity, install
the new private/public pair as `id_ed25519_dgx_cluster` on every node, verify
all six directed peer paths, then remove the old public-key line everywhere.
If any step fails, keep the old key authorized until all nodes are recovered.

The model-copy helper connects to both C2 direct-edge addresses with strict
host-key checking. Connect once to `192.168.0.2` and `192.168.1.2`, verify
that each key matches C2, and record those aliases in the user's
`known_hosts`. Neither `known_hosts`, `authorized_keys`, nor either key file
belongs in Git.

## 7. Render a local DSpark profile

The tracked profiles preserve the audited installation. For a new path or
username, generate a local ignored profile:

```bash
cd ~/sparks
scripts/configure-dspark-profile.sh --model active
```

The default output is `dspark_mia/mia-throughput.local.env`. Review at least:

- `WORKER_HOST` and `WORKER_INSTALL_DIR`;
- `CLUSTER_SSH_KEY`;
- `DSPARK_MODEL_HOST_PATH`;
- C1/C2 management addresses and rendezvous/API ports;
- the rank-specific C1-P1 and C2-P0 HCA expressions;
- `enP7s7` for NCCL socket bootstrap, TP control, and Gloo.

For an OpenClaw-oriented C1-C8 deployment, render the agent profile instead:

```bash
scripts/configure-dspark-profile.sh --profile agent --model active
```

That output is `dspark_mia/mia-agent.local.env` and selects the active
abliterated FP8 revision plus `MODEL.abliterated-fp8.lock.json` atomically. It retains the one-million
token model ceiling, port 8889, and both served model IDs while using an
isolated Compose project/rendezvous/tmp identity and a C8 graph ceiling.

Use `--model official` only when deliberately reproducing the original
`deepseek-ai/DeepSeek-V4-Flash-DSpark` reference lock. Do not hand-edit just
the model path: the renderer changes the host path, container path, repository,
revision, and model-lock selector as one profile choice.

Select exactly one rendered profile and keep it exported for every lifecycle
or provisioning command:

```bash
# OpenClaw / C1-C8:
export MIA_ENV_FILE="$HOME/sparks/dspark_mia/mia-agent.local.env"

# Or, for C1-C32 throughput waves:
# export MIA_ENV_FILE="$HOME/sparks/dspark_mia/mia-throughput.local.env"
```

Do not add the generated profile to Git. It contains no required secret, but
it describes the local paths and topology.

Sync the pinned integration and selected profile to C2:

```bash
cd ~/sparks/dspark_mia
./bin/sync-worker.sh
```

This verifies that the MiaAI-Lab submodule is clean at the locked commit,
copies the integration to the exact worker path, and compares the selected
profile hash on both machines.

## 8. Install the rootful-Docker policy

The lifecycle wrappers explicitly use `sudo -n /usr/bin/docker`; they do not
fall back to a user/rootless Docker socket.

**All three ring nodes:**

```bash
cd ~/sparks
scripts/install-docker-sudoers.sh install
scripts/install-docker-sudoers.sh status
```

This is narrower than unrestricted `NOPASSWD: ALL`, but Docker remains
root-equivalent. Keep the service account and its SSH key protected. C1 and C2
need this policy for the active TP2 service. C3 retains it only so the
independent model workloads (and the retained maintenance experiments) can
launch pinned containers after the broad bootstrap policy is removed.

## 9. Pull the exact container on all three hosts

```bash
# C1
cd ~/sparks
MIA_ENV_FILE="${MIA_ENV_FILE}" scripts/pull-dspark-container.sh describe
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/pull-dspark-container.sh --pull-all
```

The image reference comes from `dspark_mia/UPSTREAM.lock` and includes a
SHA-256 digest. `--pull-all` uses the selected cluster identity, verifies that
the exact repository digest is present on C1, C2, and C3, and requires
identical image IDs on all three. Compose uses `pull_policy: never`, so a model
start cannot silently change the runtime. C3 is not needed by production TP2,
but the ring verifier and retained three-node harness require the same local
image. `--pull-both` remains available for an intentionally two-rank-only
installation.

## 10. Download and copy the exact checkpoint

After installing the host prerequisites, install the Hugging Face CLI in its
own isolated user environment:

```bash
pipx install huggingface-hub
export PATH="$HOME/.local/bin:$PATH"
hf auth login
```

The token stays in the user's Hugging Face credential store; do not put it in
the profile, shell history, command arguments, or repository.

**C1:**

```bash
cd ~/sparks
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/download-pinned-model.sh --download

MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/sync-pinned-model-multirail.sh --sync
```

The first script requests the exact revision in the selected profile's lock.
`--model active` selects `MODEL.abliterated-fp8.lock.json` and revision
`7d02640c72a2c8127f116d3d1933ddfec5e4c0fa`; `--model official` selects
`MODEL.lock.json` and revision
`62af8fffb2f7030cac4de2f0169f5b8d1101b646`. A profile may select another
regular JSON lock directly inside `dspark_mia` with
`MIA_MODEL_LOCK=NAME.json`. The second script copies metadata once and stripes
the 48 Safetensors shards round-robin over the two logical links on the direct
C1-C2 edge. Logs are written under the user's state directory, outside the
repository.

For either checked-in lock, the validator requires:

- the exact repository and revision selected by that lock;
- 48 indexed Safetensors shards;
- exactly `166886535336` Safetensors bytes;
- locked SHA-256 and byte sizes for `config.json` and the checkpoint index;
- a DeepSeek V4 DSpark configuration with a 1,048,576-token position limit.

Alternate profile locks supply their own repository, revision, byte totals,
and key-file hashes; the same shard, metadata, and DeepSeek V4 structural
checks still apply.

Re-run local and remote validation:

```bash
cd ~/sparks/dspark_mia
./bin/validate-model.sh

remote_profile="$HOME/sparks/dspark_mia/$(basename "${MIA_ENV_FILE}")"
ssh -i ~/.ssh/id_ed25519_dgx_cluster -o IdentitiesOnly=yes cerebrus2 \
  "MIA_ENV_FILE='${remote_profile}' \
   '$HOME/sparks/dspark_mia/bin/validate-model.sh'"
```

If the two accounts have different home paths, use the worker path from the
profile in the remote command.

The active TP2 service does not read checkpoint files from C3. The retained
PP3 compatibility harness has its own lock for the active abliterated
checkpoint. To stage that checkpoint on C3, first render a portable, ignored
trial profile and keep its selector exported for every trial command:

```bash
cd ~/sparks
scripts/configure-mia3-profile.sh
export MIA3_ENV_FILE=mia3.local.env

cd dspark_mia3
./bin/sync.sh
./bin/sync-model.sh 2
./bin/preflight.sh
```

`sync-model.sh 2` copies from C1 to C3 over their direct ring edge and validates
the exact trial lock remotely. The ring NCCL verifier needs the pinned image,
not the model; stage the 166.9 GB checkpoint on C3 only when preserving the PP3
reproduction path or preparing another explicitly reviewed model trial.

## 11. Validate without launching

Stop any other GPU model service deliberately before this step. The preflight
will refuse to continue when another vLLM workload or either selected port is
active; it never stops that workload for you.

**C1:**

```bash
cd ~/sparks/dspark_mia
./bin/validate-static.sh
MIA_ENV_FILE=mia-throughput.env ./tests/test-profile-selection.sh
./tests/test-profile-renderer.sh
./tests/test-model-catalog.sh
MIA_ENV_FILE=mia-throughput.env ./tests/test-start-timeout.sh
MIA_ENV_FILE=mia-throughput.env ./tests/test-supervisor.sh
./bin/preflight.sh
```

`preflight.sh` checks both rendered Compose ranks, both pinned artifacts, both
logical links on the direct TP2 edge, SSH, Docker authority, ports, and absence of a
conflicting vLLM process. It performs no pull, download, launch, stop, or
service mutation.

## 12. Install the supervisor and dashboard

Only C1 owns the DSpark service:

```bash
cd ~/sparks
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/install-dspark-supervisor.sh start
```

Do not enable an independent model service on C2. Containers deliberately
use `restart: "no"` because a single TP rank cannot rejoin an existing NCCL
generation safely. C1's long-running supervisor detects either failed,
rebooted, OOM-killed, or replaced rank and recycles the pair in worker-first
order.

Cold initialization can take several minutes:

```bash
journalctl -fu dgx-spark-dspark-mia.service
```

When ready:

```bash
cd ~/sparks/dspark_mia
./bin/probe.sh
curl -fsS http://127.0.0.1:8889/health
curl -fsS http://127.0.0.1:8889/v1/models | jq
```

Install the read-only dashboard and Nginx front end on C1:

```bash
cd ~/sparks
DASHBOARD_WEB_HOST=cerebrus1.lan \
  scripts/install-dashboard.sh start --web
```

The public template binds the Python collector to loopback. Nginx accepts the
friendly port-80 URL and redirects it to HTTPS. `cerebrus1.lan` is both the
code default and the canonical dashboard endpoint; set `DASHBOARD_WEB_HOST`
only when the site's resolvable DNS name differs. On a fresh install the script
creates a random Basic-auth password in the mode-0600 host environment; the TLS
private key and password never enter Git. Configure `DASHBOARD_AUTH` explicitly
before any direct non-loopback collector bind.
See [`OPERATIONS.md`](OPERATIONS.md) and
[`dashboard/README.md`](../dashboard/README.md).

To keep the dashboard process and plot history off both Sparks, install the
same collector on a third Linux host after the pair is healthy. The
fixed-command SSH key, sanitized remote environment, systemd flow, and safe
cutover are in [`REMOTE_DASHBOARD.md`](REMOTE_DASHBOARD.md). The default above
remains the supported on-Spark mode.

## 13. Remove broad sudo and review network exposure

**All provisioned ring nodes:**

```bash
cd ~/sparks
scripts/install-docker-sudoers.sh status
scripts/bootstrap-sudo.sh disable
scripts/bootstrap-sudo.sh status
```

Review any other pre-existing sudo policies separately. The removal command
deletes only `/etc/sudoers.d/90-sparks-bootstrap-nopasswd`.

The selected vLLM profile listens on `0.0.0.0:8889` and has no API
authentication. The original hosts also had UFW inactive. Before attaching a
Spark to an untrusted network, restrict 8889 to the OpenClaw/agent host or
trusted management subnet with the site's firewall. Likewise restrict ports
80/443 to intended dashboard clients; do not expose the raw 8090 collector.
Preserve SSH access when changing a remote firewall.

No provider API key is required by vLLM. Any OpenClaw or client credentials
belong on the separate agent machine or in an external mode-0600 secret store,
not in either model-server checkout.

## 14. Prove boot recovery

First test recovery without rebooting by following the exact scoped procedure
in [`OPERATIONS.md`](OPERATIONS.md). It resolves the rank through both Compose
labels, kills only that verified container, and confirms that C1 replaces
both rank identities and restores the advertised model.

After that passes, schedule a reboot test. A TP pair is unavailable while
either rank host is down; C1 is the sole coordinator and will rebuild one
coherent generation when both rank hosts and their direct TP2 edge return.

Verify after the reboot:

```bash
systemctl is-enabled dgx-spark-dspark-mia.service
systemctl is-active dgx-spark-dspark-mia.service
cd ~/sparks/dspark_mia
./bin/probe.sh
CX7_NODE_ROLE=cerebrus1 ../bin/wait-cx7-ready.sh --check-once --scope tp2
```

Finally run the checks in [`VALIDATION.md`](VALIDATION.md), including the
fixed-length 1/2/4/8/16/32 matrix and RDMA-counter proof that traffic crossed
both logical links on the direct C1-P1/C2-P0 production edge.
