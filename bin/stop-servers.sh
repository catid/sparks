#!/usr/bin/env bash
set -euo pipefail

# Targets only the Laguna FP8/NVFP4 vLLM processes launched by this project,
# including the node-local NVFP4+DFlash agent backends.
stop_local() {
  pkill -INT -f \
    'vllm serve poolside/Laguna-S-2.1-(FP8|NVFP4)( |$)' \
    2>/dev/null || true
}

stop_local
ssh -i /home/catid/.ssh/id_ed25519_dgx_cluster \
  -o IdentitiesOnly=yes spark2 \
  "pkill -INT -f 'vllm serve poolside/Laguna-S-2.1-(FP8|NVFP4)( |$)' 2>/dev/null || true" || true
