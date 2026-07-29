#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${root_dir}/bin/cluster-env.sh"

mode="${1:-dflash}"
node="${DGX_REPLICA_NODE:-spark1}"
port="${VLLM_PORT:-8000}"
model="poolside/Laguna-S-2.1-NVFP4"
dflash_tokens="${DFLASH_TOKENS:-15}"

case "${mode}" in
  baseline)
    speculative_args=()
    ;;
  dflash)
    speculative_args=(
      --speculative-config
      "{\"model\":\"poolside/Laguna-S-2.1-DFlash-NVFP4\",\"num_speculative_tokens\":${dflash_tokens},\"method\":\"dflash\"}"
    )
    ;;
  *)
    echo "usage: $0 [baseline|dflash]" >&2
    exit 2
    ;;
esac

common_args=(
  "${model}"
  --tensor-parallel-size 1
  --pipeline-parallel-size 1
  --max-model-len 8192
  --max-num-seqs 32
  --max-num-batched-tokens 32768
  --gpu-memory-utilization 0.85
  --kv-cache-dtype fp8
  --generation-config vllm
  --no-enable-prefix-caching
  --host 0.0.0.0
  --port "${port}"
  "${speculative_args[@]}"
)

mkdir -p "${root_dir}/logs"
if [[ "${node}" == "spark2" ]]; then
  exec vllm serve "${common_args[@]}"
fi

remote_log="${root_dir}/logs/nvfp4-${mode}-spark2.log"
ssh -i /home/catid/.ssh/id_ed25519_dgx_cluster \
  -o IdentitiesOnly=yes spark2 \
  "nohup env DGX_REPLICA_NODE=spark2 DFLASH_TOKENS='${dflash_tokens}' VLLM_PORT='${port}' '${root_dir}/bin/launch-nvfp4-replicas.sh' '${mode}' >'${remote_log}' 2>&1 & echo \$! >'${root_dir}/logs/nvfp4-${mode}-spark2.pid'"

exec vllm serve "${common_args[@]}"
