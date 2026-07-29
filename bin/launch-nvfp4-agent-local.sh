#!/usr/bin/env bash
# shellcheck disable=SC2317  # Trap callbacks are invoked indirectly.
set -euo pipefail

# Run one complete Laguna NVFP4+DFlash replica on this Spark. This script is
# intentionally node-local so it can be used directly by systemd without SSH
# or any distributed vLLM/NCCL configuration.

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${root_dir}/bin/cluster-env.sh"

model="poolside/Laguna-S-2.1-NVFP4"
draft_model="${DFLASH_MODEL:-poolside/Laguna-S-2.1-DFlash-NVFP4}"
dflash_tokens="${DFLASH_TOKENS:-7}"
host="${LAGUNA_VLLM_HOST:-0.0.0.0}"
port="${LAGUNA_VLLM_PORT:-8000}"
max_model_len="${LAGUNA_MAX_MODEL_LEN:-262144}"
pid_file="${VLLM_AGENT_PID_FILE:-${root_dir}/logs/nvfp4-agent-$(hostname -s).pid}"

if [[ ! "${port}" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
  echo "LAGUNA_VLLM_PORT must be an integer from 1 through 65535 (got: ${port})" >&2
  exit 2
fi

if [[ ! "${dflash_tokens}" =~ ^[0-9]+$ ]] || ((dflash_tokens < 1 || dflash_tokens > 15)); then
  echo "DFLASH_TOKENS must be an integer from 1 through 15 (got: ${dflash_tokens})" >&2
  exit 2
fi

if [[ ! "${max_model_len}" =~ ^[0-9]+$ ]] || ((max_model_len < 4096 || max_model_len > 262144)); then
  echo "LAGUNA_MAX_MODEL_LEN must be an integer from 4096 through 262144 (got: ${max_model_len})" >&2
  exit 2
fi

mkdir -p "${root_dir}/logs" "$(dirname "${pid_file}")"

if [[ -s "${pid_file}" ]]; then
  existing_pid="$(<"${pid_file}")"
  if [[ "${existing_pid}" =~ ^[0-9]+$ ]] && kill -0 "${existing_pid}" 2>/dev/null; then
    existing_cmd="$(tr '\0' ' ' <"/proc/${existing_pid}/cmdline" 2>/dev/null || true)"
    if [[ "${existing_cmd}" == *"vllm serve ${model}"* ]]; then
      echo "Laguna agent backend is already running as PID ${existing_pid}" >&2
      exit 1
    fi
  fi
  rm -f "${pid_file}"
fi

serve_args=(
  "${model}"
  --tensor-parallel-size 1
  --pipeline-parallel-size 1
  --max-model-len "${max_model_len}"
  --max-num-seqs 32
  --max-num-batched-tokens 32768
  --gpu-memory-utilization 0.85
  --kv-cache-dtype fp8
  --enable-prefix-caching
  --generation-config auto
  --override-generation-config '{"temperature":0.7,"top_p":0.95}'
  --enable-auto-tool-choice
  --tool-call-parser poolside_v1
  --reasoning-parser poolside_v1
  --speculative-config
  "{\"model\":\"${draft_model}\",\"num_speculative_tokens\":${dflash_tokens},\"method\":\"dflash\"}"
  --host "${host}"
  --port "${port}"
)

vllm serve "${serve_args[@]}" &
server_pid=$!
printf '%s\n' "${server_pid}" >"${pid_file}"

shutdown_requested=0
forward_shutdown() {
  shutdown_requested=1
  kill -INT "${server_pid}" 2>/dev/null || true
}
cleanup() {
  rm -f "${pid_file}"
}
trap forward_shutdown INT TERM HUP
trap cleanup EXIT

set +e
wait "${server_pid}"
status=$?
if ((shutdown_requested)) && kill -0 "${server_pid}" 2>/dev/null; then
  wait "${server_pid}"
  status=$?
fi
set -e

exit "${status}"
