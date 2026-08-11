#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command curl
need_command ssh
require_head_host
require_ssh_identity

echo "== ${RANK2_HOST} / rank 2 =="
remote_trial_command 2 node-compose.sh 2 ps -a
echo "== ${RANK1_HOST} / rank 1 =="
remote_trial_command 1 node-compose.sh 1 ps -a
echo "== ${HEAD_HOST} / rank 0 =="
"${MIA3_ROOT}/bin/node-compose.sh" 0 ps -a
echo "== API =="
if curl -fsS --max-time 5 "http://127.0.0.1:${VLLM_PORT}/health" >/dev/null; then
  echo "healthy http://${HEAD_MGMT_IP}:${VLLM_PORT}/v1"
else
  echo "not ready on port ${VLLM_PORT}"
fi
echo "partition=${MIA3_PARTITION_PROFILE} layers=${VLLM_PP_LAYER_PARTITION:-automatic} DFlash=${ENABLE_DSPARK}"
