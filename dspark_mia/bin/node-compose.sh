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
: "${MASTER_ADDR:?runtime MASTER_ADDR must be passed explicitly}"
: "${VLLM_HOST_IP:?runtime VLLM_HOST_IP must be passed explicitly}"
if ! valid_ipv4 "${MASTER_ADDR}" || ! valid_ipv4 "${VLLM_HOST_IP}"; then
  echo "Runtime MASTER_ADDR and VLLM_HOST_IP must be IPv4 addresses." >&2
  exit 2
fi
case "$1" in
  config|down|ps)
    # These commands only render/query/remove the exact scoped Compose
    # project. They never start a rank, so interface/DNS availability must not
    # prevent observation or cleanup.
    ;;
  *)
    need_command ip
    validate_node_runtime_addresses "${rank}"
    ;;
esac
headless="$(node_headless "${rank}")"
node_hca="$(node_nccl_hca "${rank}")"

exec sudo -n /usr/bin/env \
  -u NCCL_IB_GID_INDEX \
  COMPOSE_DISABLE_ENV_FILE=1 \
  NODE_RANK="${rank}" \
  HEADLESS="${headless}" \
  MASTER_ADDR="${MASTER_ADDR}" \
  VLLM_HOST_IP="${VLLM_HOST_IP}" \
  NCCL_IB_HCA="${node_hca}" \
  /usr/bin/docker compose \
    --project-name "${MIA_PROJECT_NAME}" \
    --env-file "${MIA_ENV_FILE}" \
    --file "${MIA_UPSTREAM_COMPOSE}" \
    --file "${MIA_OVERRIDE_COMPOSE}" \
    "$@"
