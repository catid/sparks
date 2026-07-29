# Fresh two-Spark setup

This runbook reproduces the selected DeepSeek V4 Flash DSpark deployment on
two DGX Sparks without relying on files from the original hosts. It assumes:

- both machines run a current DGX OS image and use their supplied 240 W NVIDIA
  power adapters;
- the machines are named `spark1` and `spark2`, and `spark2` resolves from
  Spark 1 over a separate management network;
- the same unprivileged service user exists on both;
- that user's home and checkout paths contain no whitespace or shell
  metacharacters (the renderers reject ambiguous service paths);
- two ConnectX-7 cables join the machines in the NVIDIA-supported topology;
- each host has at least 400 GB free for the 166.9 GB checkpoint, an 18.8 GB
  container image, caches, and working headroom;
- the Hugging Face account used for provisioning can access
  `deepseek-ai/DeepSeek-V4-Flash-DSpark`.

Commands labeled **both** must be run separately on Spark 1 and Spark 2.
Commands labeled **Spark 1** coordinate the pair.

## 1. Clone and temporarily enable passwordless sudo

**Both:**

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
account with this authority to untrusted users. Section 12 replaces it with a
narrower policy and removes it.

Never copy an existing `.bashrc`, API-key file, Hugging Face token, SSH private
key, Docker auth file, OpenClaw state directory, or TLS private key into this
checkout. This is a public repository.

## 2. Verify the DGX base and install host tools

The playbook builds on DGX OS; it does not replace the vendor kernel, firmware,
NVIDIA driver, CUDA toolkit, Docker Engine, Compose plugin, or NVIDIA Container
Toolkit.

**Both:**

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

## 3. Run headlessly

**Both:**

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

## 4. Configure and validate the four ConnectX-7 rails

The two physical cables expose four logical netdev/RDMA pairs. The checked-in
Netplan files assign MTU-9000 point-to-point subnets:

| Rail | Spark 1 | Spark 2 |
| --- | --- | --- |
| `enp1s0f0np0` | `192.168.100.10/24` | `192.168.100.11/24` |
| `enP2p1s0f0np0` | `192.168.101.10/24` | `192.168.101.11/24` |
| `enp1s0f1np1` | `192.168.102.10/24` | `192.168.102.11/24` |
| `enP2p1s0f1np1` | `192.168.103.10/24` | `192.168.103.11/24` |

Dry-run validation is the default:

```bash
# Spark 1
cd ~/sparks
scripts/install-cx7-netplan.sh spark1

# Spark 2
cd ~/sparks
scripts/install-cx7-netplan.sh spark2
```

Retain console or management-network access while changing networking. When
the proposed files are correct:

```bash
# Spark 1
scripts/install-cx7-netplan.sh spark1 --apply

# Spark 2
scripts/install-cx7-netplan.sh spark2 --apply
```

Each installer backs up an existing `/etc/netplan/40-cx7.yaml`, applies only
the four CX-7 interfaces, then checks carrier, MTU, address, RDMA state, and
peer ping. Recheck either host at any time:

```bash
bin/wait-cx7-ready.sh --check-once
rdma link show
```

All four rails should report `ACTIVE/LINK_UP`. Read
[`NETWORKING.md`](NETWORKING.md) before changing interface names, addresses,
GID selection, or NCCL HCA ordering.

## 5. Create the Spark 1 to Spark 2 service identity

The systemd supervisor has no interactive SSH agent, so Spark 1 needs a
dedicated on-disk key that can log in as the service user on Spark 2.

**Spark 1:**

```bash
install -d -m 0700 ~/.ssh
ssh-keygen -t ed25519 \
  -f ~/.ssh/id_ed25519_dgx_cluster \
  -C dspark-cluster
ssh-copy-id -i ~/.ssh/id_ed25519_dgx_cluster.pub spark2
chmod 0600 ~/.ssh/id_ed25519_dgx_cluster
ssh -i ~/.ssh/id_ed25519_dgx_cluster \
  -o IdentitiesOnly=yes spark2 hostname -s
```

Use an empty key passphrase only when unattended boot recovery is required,
and compensate with a dedicated account, trusted management network, and
tightly controlled file permissions. Verify the Spark 2 host-key fingerprint
through an independent channel before accepting it.

The four-rail model-copy helper connects to each direct IP with strict host-key
checking. Connect once to `192.168.100.11` through `192.168.103.11`, verify
that each presented key matches Spark 2, and allow OpenSSH to record those
aliases in the user's `known_hosts`. Neither `known_hosts` nor either key file
belongs in Git.

## 6. Render a local DSpark profile

The tracked profiles preserve the audited installation. For a new path or
username, generate a local ignored profile:

```bash
cd ~/sparks
scripts/configure-dspark-profile.sh
```

The default output is `dspark_mia/mia-throughput.local.env`. Review at least:

- `WORKER_HOST` and `WORKER_INSTALL_DIR`;
- `CLUSTER_SSH_KEY`;
- `DSPARK_MODEL_HOST_PATH`;
- Spark 1/2 addresses and the rendezvous/API ports;
- the four NCCL HCA names.

For an OpenClaw-oriented C1-C8 deployment, render the agent profile instead:

```bash
scripts/configure-dspark-profile.sh --profile agent
```

That output is `dspark_mia/mia-agent.local.env`. It retains the one-million
token model ceiling, port 8889, and both served model IDs while using an
isolated Compose project/rendezvous/tmp identity and a C8 graph ceiling.

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

Sync the pinned integration and selected profile to Spark 2:

```bash
cd ~/sparks/dspark_mia
./bin/sync-worker.sh
```

This verifies that the MiaAI-Lab submodule is clean at the locked commit,
copies the integration to the exact worker path, and compares the selected
profile hash on both machines.

## 7. Install the rootful-Docker policy

The lifecycle wrappers explicitly use `sudo -n /usr/bin/docker`; they do not
fall back to a user/rootless Docker socket.

**Both:**

```bash
cd ~/sparks
scripts/install-docker-sudoers.sh install
scripts/install-docker-sudoers.sh status
```

This is narrower than unrestricted `NOPASSWD: ALL`, but Docker remains
root-equivalent. Keep the service account and its SSH key protected.

## 8. Pull the exact container on both hosts

```bash
# Spark 1
cd ~/sparks
MIA_ENV_FILE="${MIA_ENV_FILE}" scripts/pull-dspark-container.sh
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/pull-dspark-container.sh --pull-both
```

The image reference comes from `dspark_mia/UPSTREAM.lock` and includes a
SHA-256 digest. The paired helper uses the selected cluster identity and
requires identical image IDs on both nodes. Compose uses `pull_policy: never`,
so a model start cannot silently change the runtime.

## 9. Download and copy the exact checkpoint

After installing the host prerequisites, install the Hugging Face CLI in its
own isolated user environment:

```bash
pipx install huggingface-hub
export PATH="$HOME/.local/bin:$PATH"
hf auth login
```

The token stays in the user's Hugging Face credential store; do not put it in
the profile, shell history, command arguments, or repository.

**Spark 1:**

```bash
cd ~/sparks
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/download-pinned-model.sh --download

MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/sync-pinned-model-multirail.sh --sync
```

The first script requests the exact revision in `MODEL.lock.json`. The second
copies metadata once and stripes the 48 Safetensors shards round-robin over
all four direct links. Logs are written under the user's state directory,
outside the repository.

The validator requires:

- revision `62af8fffb2f7030cac4de2f0169f5b8d1101b646`;
- 48 indexed Safetensors shards;
- exactly `166886535336` Safetensors bytes;
- locked SHA-256 and byte sizes for `config.json` and the checkpoint index;
- a DeepSeek V4 DSpark configuration with a 1,048,576-token position limit.

Re-run local and remote validation:

```bash
cd ~/sparks/dspark_mia
./bin/validate-model.sh

remote_profile="$HOME/sparks/dspark_mia/$(basename "${MIA_ENV_FILE}")"
ssh -i ~/.ssh/id_ed25519_dgx_cluster -o IdentitiesOnly=yes spark2 \
  "MIA_ENV_FILE='${remote_profile}' \
   '$HOME/sparks/dspark_mia/bin/validate-model.sh'"
```

If the two accounts have different home paths, use the worker path from the
profile in the remote command.

## 10. Validate without launching

Stop any other GPU model service deliberately before this step. The preflight
will refuse to continue when another vLLM workload or either selected port is
active; it never stops that workload for you.

**Spark 1:**

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

`preflight.sh` checks both rendered Compose ranks, both pinned artifacts, all
four rails on both hosts, SSH, Docker authority, ports, and absence of a
conflicting vLLM process. It performs no pull, download, launch, stop, or
service mutation.

## 11. Install the supervisor and dashboard

Only Spark 1 owns the DSpark service:

```bash
cd ~/sparks
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  scripts/install-dspark-supervisor.sh start
```

Do not enable an independent model service on Spark 2. Containers deliberately
use `restart: "no"` because a single TP rank cannot rejoin an existing NCCL
generation safely. Spark 1's long-running supervisor detects either failed,
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

Install the read-only dashboard and Nginx front end on Spark 1:

```bash
cd ~/sparks
scripts/install-dashboard.sh start --web
```

The public template binds the Python collector to loopback. Nginx accepts the
friendly port-80 URL, redirects it to HTTPS, and serves `spark1.lan` on port
443 using a locally generated self-signed certificate. On a fresh install the
script creates a random Basic-auth password in the mode-0600 host environment;
the TLS private key and password never enter Git. Configure `DASHBOARD_AUTH`
explicitly before any direct non-loopback collector bind.
See [`OPERATIONS.md`](OPERATIONS.md) and
[`dashboard/README.md`](../dashboard/README.md).

## 12. Remove broad sudo and review network exposure

**Both:**

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

## 13. Prove boot recovery

First test recovery without rebooting by following the exact scoped procedure
in [`OPERATIONS.md`](OPERATIONS.md). It resolves the rank through both Compose
labels, kills only that verified container, and confirms that Spark 1 replaces
both rank identities and restores the advertised model.

After that passes, schedule a reboot test. A TP pair is unavailable while
either host is down; Spark 1 is the sole coordinator and will rebuild one
coherent generation when both hosts and all four rails return.

Verify after the reboot:

```bash
systemctl is-enabled dgx-spark-dspark-mia.service
systemctl is-active dgx-spark-dspark-mia.service
cd ~/sparks/dspark_mia
./bin/probe.sh
../bin/wait-cx7-ready.sh --check-once
```

Finally run the checks in [`VALIDATION.md`](VALIDATION.md), including the
fixed-length 1/2/4/8/16/32 matrix and RDMA-counter proof that traffic crossed
all four HCAs.
