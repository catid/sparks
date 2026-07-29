#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${root_dir}/bin/cluster-env.sh"

mode="${1:-dflash}"
rank="${DGX_NODE_RANK:-0}"
port="${VLLM_PORT:-8000}"
master_addr="${VLLM_MASTER_ADDR:-192.168.100.10}"
master_port="${VLLM_MASTER_PORT:-29501}"
dflash_tokens="${DFLASH_TOKENS:-15}"
model="poolside/Laguna-S-2.1-FP8"

case "${mode}" in
  baseline)
    speculative_args=()
    ;;
  dflash)
    speculative_args=(
      --speculative-config
      "{\"model\":\"poolside/Laguna-S-2.1-DFlash-FP8\",\"num_speculative_tokens\":${dflash_tokens},\"method\":\"dflash\"}"
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
  --pipeline-parallel-size 2
  --distributed-executor-backend mp
  --nnodes 2
  --node-rank "${rank}"
  --master-addr "${master_addr}"
  --master-port "${master_port}"
  --distributed-timeout-seconds 1800
  --max-model-len 8192
  --max-num-seqs 32
  --max-num-batched-tokens 32768
  --gpu-memory-utilization 0.90
  --kv-cache-dtype fp8
  --generation-config vllm
  --no-enable-prefix-caching
  --host 0.0.0.0
  --port "${port}"
  "${speculative_args[@]}"
)

mkdir -p "${root_dir}/logs"
if [[ "${rank}" == "1" ]]; then
  exec vllm serve "${common_args[@]}" --headless
fi

remote_log="${root_dir}/logs/fp8-${mode}-spark2.log"
ssh -i /home/catid/.ssh/id_ed25519_dgx_cluster \
  -o IdentitiesOnly=yes spark2 \
  "nohup env DGX_NODE_RANK=1 DFLASH_TOKENS='${dflash_tokens}' VLLM_PORT='${port}' VLLM_MASTER_ADDR='${master_addr}' VLLM_MASTER_PORT='${master_port}' '${root_dir}/bin/launch-fp8-pp2.sh' '${mode}' >'${remote_log}' 2>&1 & echo \$! >'${root_dir}/logs/fp8-${mode}-spark2.pid'"

exec vllm serve "${common_args[@]}"
