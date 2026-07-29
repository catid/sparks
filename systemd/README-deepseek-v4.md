# Persistent two-Spark DeepSeek V4 service

This layout runs one logical vLLM server using TP2 across the two DGX Sparks:

- Spark 1 owns API port `8000`, NCCL rendezvous port `29618`, and the cluster
  restart generation.
- Spark 2 runs a headless rank. Its service is installed but not enabled; the
  Spark 1 rank starts and stops it over the direct `192.168.100.0/24` rail.
- Both ranks wait for all four MTU-9000 RoCE links, their expected addresses,
  active RDMA state, and their direct peer before launching.
- The readiness unit is a non-resident oneshot, so every initial start and
  automatic rank-0 restart performs a fresh check. It retains
  `NoNewPrivileges=true` and receives only `CAP_NET_RAW`, which the ICMP peer
  probes require on the current hosts.

Repository files:

- `dgx-spark-cx7-ready.service`: install on both Sparks.
- `dgx-spark-deepseek-v4-rank0.service`: install and enable only on Spark 1.
- `dgx-spark-deepseek-v4-rank1.service`: install on Spark 2, but do not enable.
- `dgx-spark-deepseek-v4.env.example`: copy to
  `/etc/dgx-spark-deepseek-v4.env` on both nodes and keep inference values
  identical. The checked-in production profile runs both target and DFlash
  eager, uses `0.90` GPU-memory utilization, and auto-fits one maximum-context
  request (`DEEPSEEK_MAX_MODEL_LEN=-1`, `DEEPSEEK_MAX_NUM_SEQS=1`).
- `libexec/dgx-spark-deepseek-v4-rank1-control`: root-owned forced-command
  endpoint installed only on Spark 2.
- `security/dgx-spark-deepseek-v4-rank1-control.sudoers`: exact three-command
  sudo policy installed only on Spark 2.
- `bin/install-deepseek-v4-rank1-control.sh`: non-service installer for the
  dedicated key and its Spark 2 policy.

Synchronize this repository at the same absolute path on both hosts, then run
the side-effect-free validator on each:

```bash
/home/catid/dgx-spark-laguna/bin/validate-deepseek-v4-services.sh --live-cx7
```

It checks the shell helpers, rejects hostile SSH and protocol test values,
executes the forced-command wrapper against harmless recorders, validates the
exact sudoers policy with `visudo`, verifies the narrow ICMP capability and
per-start readiness semantics, verifies the unit files with `systemd-analyze`,
and optionally probes the live local rails. It does not install, enable, start,
or stop any unit.

## Restricted rank-1 controller

Rank 0 uses a dedicated Ed25519 key at:

```text
/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control
```

It does not use or replace the general cluster key. The corresponding Spark 2
`authorized_keys` entry is source-restricted to `192.168.100.10`, uses
OpenSSH's `restrict` option, and forces the root-owned wrapper:

```text
from="192.168.100.10",restrict,command="/usr/local/libexec/dgx-spark-deepseek-v4-rank1-control" ...
```

The controller sends one of three opaque versioned request tokens. They are
intentionally not valid Spark 2 shell commands. The wrapper accepts exact
matches only and maps them to:

- unprivileged `systemctl show` for `LoadState`, `ActiveState`, `SubState`,
  `MainPID`, and `InvocationID`;
- `sudo -n systemctl reset-failed` followed by
  `sudo -n systemctl restart --no-block`; or
- `sudo -n systemctl stop --no-block`.

Missing commands, whitespace changes, extra arguments, shell syntax, PTY
requests, forwarding, and every other SSH operation are denied. A successful
forced-mode status probe therefore verifies both SSH access and forced-wrapper
dispatch while preserving the controller's existing `Key=Value` status parser.

Spark 2 grants passwordless root permission for exactly:

```text
/usr/bin/systemctl reset-failed dgx-spark-deepseek-v4-rank1.service
/usr/bin/systemctl restart --no-block dgx-spark-deepseek-v4-rank1.service
/usr/bin/systemctl stop --no-block dgx-spark-deepseek-v4-rank1.service
```

Status is unprivileged, so it is deliberately absent from sudoers. The policy
contains no wildcard. Installing this separate policy neither edits nor
removes any existing sudoers file. Security still depends on the forced
wrapper because the existing interactive account may independently have
broader administrative rights.

`manage-deepseek-v4-rank1.sh` retains `legacy-shell-v1` as its default only so
an existing, not-yet-migrated environment keeps working. The production
environment example explicitly selects `forced-command-v1`; the rank-0 service
installer rejects the legacy protocol and the old general cluster key.

## Safe deployment sequence

Do not perform this sequence until transient benchmark ranks may be left
untouched. None of the steps starts, stops, or restarts a live model service;
steps 1-5 do not install, enable, or disable one either.

1. Synchronize this repository to the same absolute path on both nodes and run
   the side-effect-free validator on each.
2. On Spark 1, as `catid` and without sudo, create or verify the dedicated key:

   ```bash
   /home/catid/dgx-spark-laguna/bin/install-deepseek-v4-rank1-control.sh rank0-key
   ```

   The command never overwrites a complete or partial keypair. Record the
   printed SHA256 fingerprint.
3. Transfer only
   `/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control.pub` to a temporary
   file on Spark 2 using the existing administrative channel. Verify the same
   fingerprint on Spark 2 before installation.
4. On Spark 2, append the forced key and install the wrapper/policy:

   ```bash
   /home/catid/dgx-spark-laguna/bin/install-deepseek-v4-rank1-control.sh \
     rank1-policy /path/to/id_ed25519_deepseek_v4_rank1_control.pub
   ```

   This preserves all existing authorized keys and sudoers files. It refuses
   to reuse a public key already present under different options, because an
   unrestricted duplicate would defeat the forced-command boundary.
5. Ensure `/etc/dgx-spark-deepseek-v4.env` on both nodes contains:

   ```text
   DEEPSEEK_RANK1_CONTROL_PROTOCOL=forced-command-v1
   DEEPSEEK_RANK1_SSH_KEY=/home/catid/.ssh/id_ed25519_deepseek_v4_rank1_control
   ```

   Preserve identical inference settings. Keep the manually verified direct-IP
   host entry in the configured known-hosts file; do not trust an unverified
   `ssh-keyscan`.
6. Install the rank-1 unit locally on Spark 2. This checks that the wrapper,
   exact sudoers policy, and a correctly restricted authorized key are present:

   ```bash
   /home/catid/dgx-spark-laguna/bin/install-deepseek-v4-services.sh rank1
   ```

7. Install rank 0 locally on Spark 1:

   ```bash
   /home/catid/dgx-spark-laguna/bin/install-deepseek-v4-services.sh rank0
   ```

   Its read-only opaque status probe must succeed through the dedicated key
   before installation continues. Compare the environment SHA-256 printed on
   both hosts.
8. Only after the benchmark/transient ranks are intentionally stopped, perform
   the first manual persistent start or reboot.

The service installer never starts, stops, or restarts a service. It installs
the environment file with mode `0600` if it is absent, preserves an existing
one, disables future boot activation of conflicting legacy units without
deleting or masking them, and enables only rank 0. Rank 1 remains disabled and
is controlled by rank 0. The new rank units also declare defensive conflicts
with the retired model/router units.

Before enabling rank 0, verify:

- `/home/catid/.ssh` is mode `0700`, and the dedicated private key and
  known-hosts file are mode `0600`.
- The known-hosts file contains a manually verified entry for
  `192.168.100.11`.
- The dedicated key is usable without an interactive passphrase prompt at
  boot.
- Spark 2's new `/etc/sudoers.d/dgx-spark-deepseek-v4-rank1-control` passes
  `visudo -cf`.
- The forced key cannot open a shell or execute any command other than the
  three opaque controller requests.

The policy installer intentionally leaves the user's existing general SSH key
and broad sudo rule unchanged. Removing or changing either is a separate
administrative decision and is outside this deployment.

## Operational notes

The dashboard and nginx should remain independent of the model units so host
thermals remain available while the approximately 15-minute model load is in
progress. A TP2 server has only one HTTP endpoint: monitor
`http://127.0.0.1:8000` on Spark 1, while continuing to collect Spark 2 hardware
telemetry over a separately authorized read-only path.

With the production `DEEPSEEK_MAX_MODEL_LEN=-1` setting, vLLM resolves the
actual maximum from the KV cache during startup. Record that resolved value
from the rank-0 journal after every vLLM, CUDA, model, or memory-layout change;
do not infer it from the checkpoint metadata. `DEEPSEEK_MAX_NUM_SEQS=1` is an
intentional maximum-output agent profile, not the concurrency benchmark
profile.

The API binds to `0.0.0.0` in the example environment and has no authentication.
Keep port 8000 on a trusted network or add an authenticated reverse proxy.
