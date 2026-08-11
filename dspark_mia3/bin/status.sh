#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

require_head_host

echo "== ${HEAD_HOST} / rank 0 =="
set +e
"${MIA3_ROOT}/bin/node-compose.sh" 0 ps -a
local_status=$?
set -e

remote_status=0
if [[ ! -f "${CLUSTER_SSH_KEY}" ]]; then
  echo "Worker status unavailable: missing cluster SSH identity ${CLUSTER_SSH_KEY}." >&2
  remote_status=2
elif ! command -v ssh >/dev/null 2>&1; then
  echo "Worker status unavailable: ssh is not installed." >&2
  remote_status=2
else
  for rank in 1 2; do
    echo "== $(rank_host "${rank}") / rank ${rank} =="
    set +e
    remote_trial_command "${rank}" node-compose.sh "${rank}" ps -a
    status=$?
    set -e
    ((status == 0)) || remote_status=1
  done
fi

echo "== API =="
need_command curl
if curl -fsS --max-time 5 "http://127.0.0.1:${VLLM_PORT}/health" >/dev/null; then
  echo "healthy http://${HEAD_HOST}.local:${VLLM_PORT}/v1"
else
  echo "not ready on port ${VLLM_PORT}"
fi
echo "partition=${MIA3_PARTITION_PROFILE} layers=${VLLM_PP_LAYER_PARTITION:-automatic} DFlash=${ENABLE_DSPARK}"
if ((local_status != 0)); then
  echo "Head rank status failed with status ${local_status}." >&2
fi
if ((remote_status != 0)); then
  echo "One or more worker statuses are unavailable; head status above remains authoritative." >&2
fi
((local_status == 0 && remote_status == 0))
