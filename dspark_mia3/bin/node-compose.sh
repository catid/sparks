#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

rank="${1:-}"
if [[ ! "${rank}" =~ ^[012]$ ]]; then
  echo "Usage: $0 {0|1|2} <docker compose arguments...>" >&2
  exit 2
fi
shift
if (($# == 0)); then
  echo "Usage: $0 {0|1|2} <docker compose arguments...>" >&2
  exit 2
fi

need_command sudo
host_ip="$(rank_mgmt_ip "${rank}")"
headless="$(rank_headless "${rank}")"

exec sudo -n /usr/bin/env \
  -u NCCL_IB_GID_INDEX \
  COMPOSE_DISABLE_ENV_FILE=1 \
  NODE_RANK="${rank}" \
  HEADLESS="${headless}" \
  VLLM_HOST_IP="${host_ip}" \
  ENABLE_DSPARK="${ENABLE_DSPARK}" \
  VLLM_PP_LAYER_PARTITION="${VLLM_PP_LAYER_PARTITION}" \
  /usr/bin/docker compose \
    --project-name "${MIA_PROJECT_NAME}" \
    --env-file "${MIA3_ENV_FILE}" \
    --file "${MIA3_COMPOSE_FILE}" \
    "$@"
