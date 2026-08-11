#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

rank="${1:-}"
if [[ "${rank}" != "0" && "${rank}" != "1" ]]; then
  echo "Usage: $0 {0|1} <docker compose arguments...>" >&2
  exit 2
fi
shift
if (($# == 0)); then
  echo "Usage: $0 {0|1} <docker compose arguments...>" >&2
  exit 2
fi

need_command docker
need_command sudo
host_ip="$(node_host_ip "${rank}")"
headless="$(node_headless "${rank}")"
node_hca="$(node_nccl_hca "${rank}")"

exec sudo -n /usr/bin/env \
  -u NCCL_IB_GID_INDEX \
  COMPOSE_DISABLE_ENV_FILE=1 \
  NODE_RANK="${rank}" \
  HEADLESS="${headless}" \
  VLLM_HOST_IP="${host_ip}" \
  NCCL_IB_HCA="${node_hca}" \
  /usr/bin/docker compose \
    --project-name "${MIA_PROJECT_NAME}" \
    --env-file "${MIA_ENV_FILE}" \
    --file "${MIA_UPSTREAM_COMPOSE}" \
    --file "${MIA_OVERRIDE_COMPOSE}" \
    "$@"
