#!/usr/bin/env bash
set -euo pipefail

attempt=0
while ! docker info >/dev/null 2>&1; do
  ((attempt += 1))
  if [[ -S /var/run/docker.sock &&
        ( ! -r /var/run/docker.sock || ! -w /var/run/docker.sock ) ]]; then
    echo "Docker socket exists but is not usable by uid ${EUID}." >&2
    exit 2
  fi
  if ((attempt == 1 || attempt % 12 == 0)); then
    echo "Waiting for the Docker daemon (${attempt})..." >&2
  fi
  if [[ -n "${NOTIFY_SOCKET:-}" ]]; then
    systemd-notify --status="Waiting for the Docker daemon" || true
  fi
  sleep 5
done
