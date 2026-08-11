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
case "$1:${MIA3_RENDER_LAUNCH_CONFIG:-0}" in
  config:0|down:*|ps:*)
    # Compose interpolates these values even though non-launch commands never
    # bind or rendezvous. Documentation-only addresses keep local inspection
    # and teardown independent of DNS and peer availability.
    master_ip="192.0.2.10"
    case "${rank}" in
      0) host_ip="192.0.2.10" ;;
      1) host_ip="192.0.2.11" ;;
      2) host_ip="192.0.2.12" ;;
    esac
    ;;
  config:1|*)
    resolve_management_plane
    host_ip="$(rank_runtime_ipv4 "${rank}")"
    master_ip="$(rank_runtime_ipv4 0)"
    ;;
esac
headless="$(rank_headless "${rank}")"

exec sudo -n /usr/bin/env \
  -u NCCL_IB_GID_INDEX \
  COMPOSE_DISABLE_ENV_FILE=1 \
  NODE_RANK="${rank}" \
  HEADLESS="${headless}" \
  VLLM_HOST_IP="${host_ip}" \
  MASTER_ADDR="${master_ip}" \
  ENABLE_DSPARK="${ENABLE_DSPARK}" \
  VLLM_PP_LAYER_PARTITION="${VLLM_PP_LAYER_PARTITION}" \
  /usr/bin/docker compose \
    --project-name "${MIA_PROJECT_NAME}" \
    --env-file "${MIA3_ENV_FILE}" \
    --file "${MIA3_COMPOSE_FILE}" \
    "$@"
