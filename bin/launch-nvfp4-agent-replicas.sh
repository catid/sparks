#!/usr/bin/env bash
set -euo pipefail

# Convenience launcher for interactive testing. Production/autostart uses the
# node-local systemd service on each Cerberus node and has no SSH dependency.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
local_launcher="${root_dir}/bin/launch-nvfp4-agent-local.sh"
port="${LAGUNA_VLLM_PORT:-8000}"
cerberus2_host="${CERBERUS2_SSH_HOST:-${SPARK2_SSH_HOST:-cerberus2.local}}"
ssh_key="${CLUSTER_SSH_KEY:-/home/catid/.ssh/id_ed25519_dgx_cluster}"

if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  echo "LAGUNA_VLLM_PORT must be an integer from 1 through 65535 (got: ${port})" >&2
  exit 2
fi

mkdir -p "${root_dir}/logs"
remote_log="${root_dir}/logs/nvfp4-agent-cerberus2.log"

ssh -i "${ssh_key}" \
  -o IdentitiesOnly=yes "${cerberus2_host}" \
  "nohup env LAGUNA_VLLM_PORT='${port}' '${local_launcher}' >'${remote_log}' 2>&1 </dev/null &"

exec env LAGUNA_VLLM_PORT="${port}" "${local_launcher}"
