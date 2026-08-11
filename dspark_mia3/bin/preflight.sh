#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ss
need_command sha256sum
need_command ssh
need_command sudo
require_head_host
require_ssh_identity

local_digest="$("${MIA3_ROOT}/bin/tree-digest.sh")"
[[ -f "${MIA3_READINESS_HELPER}" && ! -L "${MIA3_READINESS_HELPER}" ]] || {
  echo "Missing regular ring readiness helper: ${MIA3_READINESS_HELPER}" >&2
  exit 1
}
local_readiness_sha="$(sha256sum "${MIA3_READINESS_HELPER}" | awk '{print $1}')"
remote_readiness_helper="$(dirname -- "${REMOTE_INSTALL_DIR}")/bin/wait-cx7-ready.sh"
"${MIA3_ROOT}/bin/validate-static.sh"

for rank in 2 1; do
  host="$(rank_host "${rank}")"
  remote_digest="$(ssh_command "${host}" "${REMOTE_INSTALL_DIR}/bin/tree-digest.sh")"
  [[ "${remote_digest}" == "${local_digest}" ]] || {
    echo "${host}: integration is not synchronized; run bin/sync.sh." >&2
    exit 1
  }
  remote_readiness_sha="$(ssh_command "${host}" sha256sum "${remote_readiness_helper}" | awk '{print $1}')"
  [[ "${remote_readiness_sha}" == "${local_readiness_sha}" ]] || {
    echo "${host}: parent-checkout ring readiness helper differs; synchronize the repository separately." >&2
    exit 1
  }
  remote_trial_command "${rank}" validate-static.sh
done

"${MIA3_ROOT}/bin/check-fabric.sh" 0
remote_trial_command 2 check-fabric.sh 2
remote_trial_command 1 check-fabric.sh 1

listener_check='for port in "$@"; do if ss -ltn "( sport = :${port} )" | tail -n +2 | grep -q .; then echo "Port ${port} is in use on $(hostname -s)." >&2; exit 1; fi; done'
workload_check='if pgrep -af "[v]llm (serve|entrypoints)" >/dev/null; then echo "A vLLM workload is active on $(hostname -s). Stop it explicitly before the trial." >&2; exit 1; fi'
image_check='sudo -n docker image inspect "$1" >/dev/null'

bash -c "${listener_check}" -- "${VLLM_PORT}" "${MASTER_PORT}"
bash -c "${workload_check}"
sudo -n docker image inspect "${DSPARK_VLLM_IMAGE}" >/dev/null || {
  echo "Pinned image is not local on ${HEAD_HOST}; preflight never pulls." >&2
  exit 1
}
"${MIA3_ROOT}/bin/check-nccl-runtime.sh"
"${MIA3_ROOT}/bin/validate-model.sh"

for rank in 2 1; do
  host="$(rank_host "${rank}")"
  ssh_command "${host}" bash -c "${listener_check}" -- "${VLLM_PORT}" "${MASTER_PORT}"
  ssh_command "${host}" bash -c "${workload_check}"
  if ! ssh_command "${host}" bash -c "${image_check}" -- "${DSPARK_VLLM_IMAGE}"; then
    echo "Pinned image is not local on ${host}; preflight never pulls." >&2
    exit 1
  fi
  remote_trial_command "${rank}" check-nccl-runtime.sh
  remote_trial_command "${rank}" validate-model.sh
done

echo "Three-node preflight passed; no container or service was changed."
if [[ "${ENABLE_DSPARK}" == 1 ]]; then
  echo "Warning: native DSpark speculation with PP is an expected compatibility trial; the pinned runtime previously rejected PP2." >&2
fi
