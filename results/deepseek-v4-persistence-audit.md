# DeepSeek V4 persistence audit

Date: 2026-07-29 UTC

Scope: repository-only review and hardening of the two-Spark persistent service
layout. No unit was installed, enabled, started, stopped, or restarted. No
sudoers, `authorized_keys`, SSH key, or live benchmark service was changed.

## Findings fixed

- The CX-7 readiness service originally combined `User=catid` and
  `NoNewPrivileges=true` with ICMP probes. This host disables unprivileged ping
  sockets (`net.ipv4.ping_group_range=1 0`), so `/usr/bin/ping` could not acquire
  its file capability and the readiness gate would wait forever.
  `dgx-spark-cx7-ready.service` now keeps no-new-privileges and grants only
  bounded and ambient `CAP_NET_RAW`.
- The readiness oneshot originally used `RemainAfterExit=yes`. Once it passed,
  subsequent model restarts would not revalidate the rails. It is now a
  non-resident oneshot, so every rank start and automatic restart pulls in a
  fresh four-rail check.
- `stop-wait` sent its remote stop request only once. A transient SSH failure
  could leave rank 1 running. It now retries the request within the configured
  stop timeout.
- Rank 0's stop timeout was shorter than the controller's 330-second remote
  cgroup wait. It is now six minutes.
- The root README and `bin/install-services.sh` still led to the legacy pair of
  independent TP1 replicas and router. A dedicated role-local installer,
  `bin/install-deepseek-v4-services.sh {rank1|rank0}`, is now the documented
  path. The legacy installer remains available but requires the explicit
  `--legacy-two-replica` argument.
- The new installer validates role/hostname, required repository and vLLM
  files, and the static service layout. Rank 0 refuses installation unless the
  rank-1 unit is already loaded over strict-host-key SSH. Units are installed
  root-owned, the environment is installed as mode `0600` if absent, legacy
  boot activation is disabled without stopping or deleting legacy services,
  only rank 0 is enabled, and the environment SHA-256 is printed for comparison.
- Static validation now covers both installers and itself, hostile SSH host and
  user values, relative key paths, the narrow ICMP capability, and the
  non-persistent readiness semantics.

## Validation performed

- `bash -n`: passed.
- ShellCheck: passed.
- `systemd-analyze verify`: passed for all three new units.
- Static persistent-service validator: passed.
- Live, read-only four-rail check: passed.
- An emulated no-new-privileges process without a capability failed ping as
  expected; the same process with only bounded and ambient `CAP_NET_RAW`
  succeeded.
- New-installer wrong-host guard: exited with status 2.
- Legacy-installer no-argument guard: exited with status 2.
- After validation, the new units remained uninstalled, legacy model/router
  units remained disabled, and the dashboard remained active.

## Security and deployment blockers

- Spark 2's current `catid` account reports `NOPASSWD: ALL`, and the current
  cluster SSH key accepts arbitrary remote commands. This is functionally
  sufficient but is not an acceptable unattended-production boundary.
  Repository-only hardening is now prepared: a dedicated key, source-restricted
  forced-command wrapper, and exact three-command sudoers policy. It has not
  been installed. The production installer rejects the legacy key/protocol, so
  the new policy remains a live deployment prerequisite.
- Existing SSH filesystem permissions are sound: `.ssh` is mode `0700`, the key
  and known-hosts file are mode `0600`, the direct-IP host key is present and
  verified, and batch SSH with the explicit identity and no agent works.
- Spark 2 did not yet contain the new persistence files at audit completion.
  Synchronize the repository before running the rank-1 installer.
- Install rank 1 on Spark 2 first, then rank 0 on Spark 1, and compare the
  printed environment hashes before rebooting.
- Stop transient benchmark ranks before the first persistent-service start or
  reboot.
- The CX-7 gate intentionally waits indefinitely while a required rail is
  absent. Rank 0 intentionally limits repeated failures to three per hour.
- The example vLLM API binds to `0.0.0.0:8000` without authentication and must
  remain on a trusted network or behind an authenticated reverse proxy.

## Follow-on forced-command hardening

The prepared controller uses opaque versioned SSH requests. They are not valid
remote shell commands, so a successful status probe proves that the dedicated
key was matched to the forced wrapper. The wrapper accepts only:

- a fixed, unprivileged five-property `systemctl show`;
- exact `reset-failed` plus `restart --no-block` for the rank-1 unit; or
- exact `stop --no-block` for the rank-1 unit.

All other original commands and wrapper arguments fail closed with status 126.
The Spark 2 installer appends the new source-restricted key, refuses an
unrestricted duplicate of the same public key, and installs root-owned wrapper
and sudoers files. It does not remove or edit any existing key or sudoers rule.
The legacy manager protocol remains the default only for migration; the service
environment and production installer require `forced-command-v1`.

Additional repository-local validation passed:

- ShellCheck and `bash -n` for the controller, hardening installer, wrapper,
  service installer, validator, and wrapper tests.
- `visudo -cf` for the exact sudoers policy.
- Mocked execution of all allowed wrapper requests, including a failed
  `reset-failed`, with exact argument assertions.
- Denial tests for missing commands, arbitrary shell commands, trailing
  whitespace, extra wrapper arguments, and newline injection.
- A temporary-key regression that forces `ssh-keygen -y` to return a trailing
  private-key comment and verifies that keypair matching compares only the
  Ed25519 type and public-key blob.
- The initial repository-only post-check found no dedicated key or installed
  policy on Spark 1. A later deliberate `rank0-key` deployment created the
  dedicated keypair and exposed the trailing-comment compatibility issue above.
  The regression fix was then validated in temporary files only; no Spark 2
  policy or model-service state was touched by that follow-up.
