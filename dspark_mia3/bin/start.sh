#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command curl
need_command jq
need_command ssh
require_head_host
require_ssh_identity
acquire_lifecycle_lock

wait_attempts="${MIA3_WAIT_ATTEMPTS:-240}"
wait_seconds="${MIA3_WAIT_SECONDS:-15}"
[[ "${wait_attempts}" =~ ^[1-9][0-9]*$ && "${wait_seconds}" =~ ^[0-9]+$ ]] || {
  echo "MIA3_WAIT_ATTEMPTS must be positive and MIA3_WAIT_SECONDS non-negative." >&2
  exit 2
}

"${MIA3_ROOT}/bin/sync.sh"
"${MIA3_ROOT}/bin/preflight.sh"

rank2_started=0
rank1_started=0
rank0_started=0
rollback() {
  local status="${1:-1}"
  trap - ERR
  echo "Three-node launch failed; removing only project ${MIA_PROJECT_NAME}." >&2
  if ((rank0_started)); then
    "${MIA3_ROOT}/bin/node-compose.sh" 0 down --timeout 30 || true
  fi
  if ((rank1_started)); then
    remote_trial_command 1 node-compose.sh 1 down --timeout 30 || true
  fi
  if ((rank2_started)); then
    remote_trial_command 2 node-compose.sh 2 down --timeout 30 || true
  fi
  exit "${status}"
}
trap 'rollback $?' ERR

echo "Starting worker rank 2 on ${RANK2_HOST}..."
rank2_started=1
remote_trial_command 2 node-compose.sh 2 up -d --no-build --pull never

echo "Starting worker rank 1 on ${RANK1_HOST}..."
rank1_started=1
remote_trial_command 1 node-compose.sh 1 up -d --no-build --pull never

echo "Starting API rank 0 on ${HEAD_HOST}..."
rank0_started=1
"${MIA3_ROOT}/bin/node-compose.sh" 0 up -d --no-build --pull never

api_url="http://127.0.0.1:${VLLM_PORT}/v1/models"
for ((attempt=1; attempt<=wait_attempts; attempt++)); do
  if ! "${MIA3_ROOT}/bin/ranks-running.sh"; then
    echo "A PP3 rank exited before API readiness." >&2
    rollback 1
  fi
  if catalog="$(curl -fsS --max-time 5 "${api_url}" 2>/dev/null)"; then
    missing=0
    while IFS= read -r model_id; do
      if ! jq -e --arg id "${model_id}" 'any(.data[]?; .id == $id)' <<<"${catalog}" >/dev/null; then
        echo "Ready API does not advertise required model ID: ${model_id}" >&2
        missing=1
      fi
    done < <(served_model_ids)
    if ((missing == 0)); then
      trap - ERR
      echo "Three-node API ready: http://${HEAD_MGMT_IP}:${VLLM_PORT}/v1"
      echo "partition=${MIA3_PARTITION_PROFILE} DFlash=${ENABLE_DSPARK}"
      exit 0
    fi
  fi
  if ((attempt % 4 == 0)); then
    echo "Still loading (${attempt}/${wait_attempts}); all three ranks remain up."
  fi
  sleep "${wait_seconds}"
done

echo "Timed out waiting for ${api_url}." >&2
rollback 1
