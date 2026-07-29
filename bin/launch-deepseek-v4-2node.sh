#!/usr/bin/env bash
set -euo pipefail

# Launch one rank of the two-Spark DeepSeek V4 Flash NVFP4 server. Start rank 1
# on Spark 2 first with --headless, then rank 0 on Spark 1.
#
# Usage:
#   DGX_NODE_RANK=1 launch-deepseek-v4-2node.sh tp2-baseline
#   DGX_NODE_RANK=0 launch-deepseek-v4-2node.sh tp2-baseline
#
# Modes: tp2-baseline, tp2-dflash, pp2-baseline, pp2-dflash
# Set DEEPSEEK_DFLASH_ENFORCE_EAGER=1 to keep only the DFlash drafter eager;
# DEEPSEEK_ENFORCE_EAGER independently controls the target model.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export USE_NCCL_230="${USE_NCCL_230:-1}"
# shellcheck disable=SC1091
source "${root_dir}/bin/cluster-env.sh"

mode="${1:-}"
rank="${DGX_NODE_RANK:-}"
target="${DEEPSEEK_TARGET:-/home/catid/models/DeepSeek-V4-Flash-NVFP4}"
drafter="${DEEPSEEK_DFLASH:-/home/catid/models/DeepSeek-V4-Flash-speculator.dflash}"
served_model="${DEEPSEEK_SERVED_MODEL:-deepseek-v4-flash-nvfp4}"
master_addr="${VLLM_MASTER_ADDR:-192.168.100.10}"
master_port="${VLLM_MASTER_PORT:-29601}"
api_host="${VLLM_HOST:-0.0.0.0}"
api_port="${VLLM_PORT:-8000}"
max_model_len="${DEEPSEEK_MAX_MODEL_LEN:-8192}"
max_num_seqs="${DEEPSEEK_MAX_NUM_SEQS:-32}"
max_num_batched_tokens="${DEEPSEEK_MAX_BATCHED_TOKENS:-32768}"
gpu_memory_utilization="${DEEPSEEK_GPU_MEMORY_UTILIZATION:-0.90}"
enable_chunked_prefill="${DEEPSEEK_ENABLE_CHUNKED_PREFILL:-1}"
load_format="${DEEPSEEK_LOAD_FORMAT:-auto}"
enforce_eager="${DEEPSEEK_ENFORCE_EAGER:-0}"
dflash_enforce_eager="${DEEPSEEK_DFLASH_ENFORCE_EAGER:-0}"
disable_hybrid_kv="${DEEPSEEK_DISABLE_HYBRID_KV:-0}"
moe_backend="${DEEPSEEK_MOE_BACKEND:-flashinfer_cutlass}"
linear_backend="${DEEPSEEK_LINEAR_BACKEND:-}"
dry_run="${DEEPSEEK_DRY_RUN:-0}"

if [[ "${rank}" != "0" && "${rank}" != "1" ]]; then
  echo "DGX_NODE_RANK must be 0 or 1." >&2
  exit 2
fi
if [[ "${dflash_enforce_eager}" != "0" && "${dflash_enforce_eager}" != "1" ]]; then
  echo "DEEPSEEK_DFLASH_ENFORCE_EAGER must be 0 or 1." >&2
  exit 2
fi
if [[ "${dflash_enforce_eager}" == "1" ]]; then
  dflash_enforce_eager_json=true
else
  dflash_enforce_eager_json=false
fi
case "${mode}" in
  tp2-baseline)
    tensor_parallel=2
    pipeline_parallel=1
    speculative_args=()
    ;;
  tp2-dflash)
    tensor_parallel=2
    pipeline_parallel=1
    speculative_args=(
      --speculative-config
      "{\"method\":\"dflash\",\"model\":\"${drafter}\",\"num_speculative_tokens\":7,\"draft_tensor_parallel_size\":2,\"attention_backend\":\"FLASH_ATTN\",\"kv_cache_dtype\":\"bfloat16\",\"enforce_eager\":${dflash_enforce_eager_json}}"
    )
    ;;
  pp2-baseline)
    tensor_parallel=1
    pipeline_parallel=2
    speculative_args=()
    ;;
  pp2-dflash)
    tensor_parallel=1
    pipeline_parallel=2
    speculative_args=(
      --speculative-config
      "{\"method\":\"dflash\",\"model\":\"${drafter}\",\"num_speculative_tokens\":7,\"draft_tensor_parallel_size\":1,\"attention_backend\":\"FLASH_ATTN\",\"kv_cache_dtype\":\"bfloat16\",\"enforce_eager\":${dflash_enforce_eager_json}}"
    )
    ;;
  *)
    echo "Usage: $0 {tp2-baseline|tp2-dflash|pp2-baseline|pp2-dflash}" >&2
    exit 2
    ;;
esac

if [[ ! -d "${target}" ]]; then
  echo "Target checkpoint not found: ${target}" >&2
  exit 2
fi
if [[ "${mode}" == *-dflash && ! -d "${drafter}" ]]; then
  echo "DFlash checkpoint not found: ${drafter}" >&2
  exit 2
fi
if [[ ! "${max_model_len}" =~ ^([0-9]+|auto|-1)$ ]]; then
  echo "DEEPSEEK_MAX_MODEL_LEN must be an integer, auto, or -1." >&2
  exit 2
fi
if [[ "${enable_chunked_prefill}" != "0" && "${enable_chunked_prefill}" != "1" ]]; then
  echo "DEEPSEEK_ENABLE_CHUNKED_PREFILL must be 0 or 1." >&2
  exit 2
fi
if [[ "${enforce_eager}" != "0" && "${enforce_eager}" != "1" ]]; then
  echo "DEEPSEEK_ENFORCE_EAGER must be 0 or 1." >&2
  exit 2
fi
if [[ "${disable_hybrid_kv}" != "0" && "${disable_hybrid_kv}" != "1" ]]; then
  echo "DEEPSEEK_DISABLE_HYBRID_KV must be 0 or 1." >&2
  exit 2
fi
if [[ ! "${moe_backend}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "DEEPSEEK_MOE_BACKEND must be a non-empty backend name." >&2
  exit 2
fi
if [[ -n "${linear_backend}" && ! "${linear_backend}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "DEEPSEEK_LINEAR_BACKEND must be empty or a backend name." >&2
  exit 2
fi
if [[ "${dry_run}" != "0" && "${dry_run}" != "1" ]]; then
  echo "DEEPSEEK_DRY_RUN must be 0 or 1." >&2
  exit 2
fi

# Four logical RoCE devices span the two physical ConnectX-7 cables. Explicit
# rail IDs keep corresponding f0/f1 paths paired across the two hosts.
export NCCL_IB_DISABLE=0
export NCCL_IB_HCA='=rocep1s0f0:1:0,roceP2p1s0f0:1:0,rocep1s0f1:1:1,roceP2p1s0f1:1:1'
export NCCL_NETDEVS_POLICY=ALL
export NCCL_CROSS_NIC=0
export NCCL_IB_MERGE_NICS=0
export NCCL_SOCKET_IFNAME='=enp1s0f0np0'
export NCCL_SOCKET_FAMILY=AF_INET
export GLOO_SOCKET_IFNAME=enp1s0f0np0
export NCCL_DMABUF_ENABLE=1
export NCCL_NET_GDR_C2C=1
# QP=4 measured about 5% slower than the default on this exact pair.
export NCCL_IB_QPS_PER_CONNECTION=1
export NCCL_IB_SPLIT_DATA_ON_QPS=0
export NCCL_DEBUG="${DEEPSEEK_NCCL_DEBUG:-INFO}"
export NCCL_DEBUG_SUBSYS="${NCCL_DEBUG_SUBSYS:-INIT,NET,GRAPH}"
export NCCL_DEBUG_FILE="${NCCL_DEBUG_FILE:-${root_dir}/logs/deepseek-v4-nccl-${mode}-rank${rank}.log}"
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1
unset NCCL_IB_GID_INDEX

mkdir -p "${root_dir}/logs"
echo "mode=${mode} rank=${rank} tp=${tensor_parallel} pp=${pipeline_parallel}"
echo "target=${target}"
echo "max_model_len=${max_model_len} max_num_seqs=${max_num_seqs} max_num_batched_tokens=${max_num_batched_tokens}"
echo "load_format=${load_format}"
echo "target_enforce_eager=${enforce_eager} dflash_enforce_eager=${dflash_enforce_eager}"
echo "disable_hybrid_kv=${disable_hybrid_kv}"
echo "moe_backend=${moe_backend} linear_backend=${linear_backend:-auto}"

serve_args=(
  "${target}"
  --served-model-name "${served_model}"
  --tensor-parallel-size "${tensor_parallel}"
  --pipeline-parallel-size "${pipeline_parallel}"
  --distributed-executor-backend mp
  --nnodes 2
  --node-rank "${rank}"
  --master-addr "${master_addr}"
  --master-port "${master_port}"
  --distributed-timeout-seconds 1800
  --trust-remote-code
  --load-format "${load_format}"
  --kv-cache-dtype fp8
  --block-size 256
  --max-model-len "${max_model_len}"
  --max-num-seqs "${max_num_seqs}"
  --max-num-batched-tokens "${max_num_batched_tokens}"
  --gpu-memory-utilization "${gpu_memory_utilization}"
  --moe-backend "${moe_backend}"
  --tokenizer-mode deepseek_v4
  --reasoning-parser deepseek_v4
  --tool-call-parser deepseek_v4
  --enable-auto-tool-choice
  --generation-config auto
  --no-enable-prefix-caching
  --compilation-config '{"cudagraph_capture_sizes":[1,2,4,8,16,32,64,128,256]}'
  --host "${api_host}"
  --port "${api_port}"
  "${speculative_args[@]}"
)

if [[ -n "${linear_backend}" ]]; then
  serve_args+=(--linear-backend "${linear_backend}")
fi

if [[ "${enable_chunked_prefill}" == "1" ]]; then
  serve_args+=(--enable-chunked-prefill)
else
  serve_args+=(--no-enable-chunked-prefill)
fi

if [[ "${enforce_eager}" == "1" ]]; then
  serve_args+=(--enforce-eager)
fi

if [[ "${disable_hybrid_kv}" == "1" ]]; then
  serve_args+=(--disable-hybrid-kv-cache-manager)
fi

if [[ "${rank}" == "1" ]]; then
  serve_args+=(--headless)
fi

if [[ "${dry_run}" == "1" ]]; then
  printf 'vllm serve'
  printf ' %q' "${serve_args[@]}"
  printf '\n'
  exit 0
fi

exec vllm serve "${serve_args[@]}"
