#!/usr/bin/env bash
set -euo pipefail

cerberus2_host="${CERBERUS2_SSH_HOST:-${SPARK2_SSH_HOST:-cerberus2.local}}"
ssh_key="${CLUSTER_SSH_KEY:-/home/catid/.ssh/id_ed25519_dgx_cluster}"

# Targets only the Laguna FP8/NVFP4 vLLM processes launched by this project,
# including the node-local NVFP4+DFlash agent backends.
stop_local() {
  pkill -INT -f \
    'vllm serve poolside/Laguna-S-2.1-(FP8|NVFP4)( |$)' \
    2>/dev/null || true
}

stop_local
ssh -i "${ssh_key}" \
  -o IdentitiesOnly=yes "${cerberus2_host}" \
  "pkill -INT -f 'vllm serve poolside/Laguna-S-2.1-(FP8|NVFP4)( |$)' 2>/dev/null || true" || true
