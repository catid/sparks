#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command curl
need_command ssh
require_ssh_identity
acquire_lifecycle_locks

helper_dir="${MIA_START_HELPER_DIR:-${MIA_ROOT}/bin}"
wait_attempts="${MIA_WAIT_ATTEMPTS:-120}"
wait_seconds="${MIA_WAIT_SECONDS:-15}"
if [[ ! "${wait_attempts}" =~ ^[1-9][0-9]*$ ||
      ! "${wait_seconds}" =~ ^[0-9]+$ ]]; then
  echo "MIA_WAIT_ATTEMPTS must be positive and MIA_WAIT_SECONDS non-negative." >&2
  exit 2
fi

require_mia_head_host

"${helper_dir}/validate-static.sh"
"${helper_dir}/sync-worker.sh"
"${helper_dir}/preflight.sh"
load_tp2_runtime_addresses "${helper_dir}"
remote_runtime_env="$(remote_runtime_assignment)"

worker_started=0
head_started=0
# shellcheck disable=SC2317  # ERR-trap callback is invoked indirectly.
rollback_on_error() {
  local status="${1:-1}"
  trap - ERR
  echo "Pinned launch failed; removing only project ${MIA_PROJECT_NAME}." >&2
  if ((head_started)); then
    "${helper_dir}/node-compose.sh" 0 down --timeout 30 || true
  fi
  if ((worker_started)); then
    ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
      "env ${remote_runtime_env} '${WORKER_INSTALL_DIR}/bin/node-compose.sh' 1 down --timeout 30" || true
  fi
  exit "${status}"
}
trap rollback_on_error ERR

echo "Starting pinned worker rank 1 on ${WORKER_HOST}..."
worker_started=1
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "env ${remote_runtime_env} '${WORKER_INSTALL_DIR}/bin/node-compose.sh' 1 up -d --no-build --pull never"

echo "Starting pinned head rank 0 on cerberus1..."
head_started=1
"${helper_dir}/node-compose.sh" 0 up -d --no-build --pull never

api_url="http://127.0.0.1:${VLLM_PORT}/v1/models"
for _ in $(seq 1 "${wait_attempts}"); do
  if ! "${helper_dir}/ranks-running.sh"; then
    echo "A TP2 rank exited before the model API became ready." >&2
    rollback_on_error 1
  fi
  if curl -fsS --max-time 5 "${api_url}" >/dev/null 2>&1; then
    trap - ERR
    echo "Pinned Mia DSpark API is ready: http://cerberus1:${VLLM_PORT}/v1"
    if [[ -n "${INVOCATION_ID:-}" ]]; then
      echo "Launch is owned by dgx-spark-dspark-mia.service."
    else
      echo "Direct launch complete; use the optional systemd unit for boot persistence."
    fi
    exit 0
  fi
  sleep "${wait_seconds}"
done

echo "Timed out waiting for ${api_url}." >&2
rollback_on_error 1
