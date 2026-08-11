#!/usr/bin/env bash
set -euo pipefail

integration_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(cd "${integration_root}/.." && pwd -P)"
agent_basename="mia-agent-render-test-${$}.env"
throughput_basename="mia-throughput-render-test-${$}.env"
invalid_basename="mia-invalid-render-test-${$}.env"
official_basename="mia-official-render-test-${$}.env"
deprecated_basename="mia-deprecated-render-test-${$}.env"
agent_path="${integration_root}/${agent_basename}"
throughput_path="${integration_root}/${throughput_basename}"
invalid_path="${integration_root}/${invalid_basename}"
official_path="${integration_root}/${official_basename}"
deprecated_path="${integration_root}/${deprecated_basename}"

cleanup() {
  rm -f -- \
    "${agent_path}" "${throughput_path}" "${invalid_path}" \
    "${official_path}" "${deprecated_path}"
}
trap cleanup EXIT

DSPARK_PROFILE_NAME="${agent_basename}" \
  "${repo_root}/scripts/configure-dspark-profile.sh" --profile agent >/dev/null

(
  unset MASTER_ADDR VLLM_HOST_IP WORKER_VLLM_HOST_IP
  set -a
  # shellcheck disable=SC1090
  source "${agent_path}"
  set +a
  [[ "${MIA_PROJECT_NAME}" == "mia-dspark-agent" ]]
  [[ "${HEAD_HOST}" == "cerberus1" ]]
  [[ "${WORKER_HOST}" == "cerberus2" ]]
  [[ -z "${MASTER_ADDR+x}" ]]
  [[ -z "${VLLM_HOST_IP+x}" ]]
  [[ -z "${WORKER_VLLM_HOST_IP+x}" ]]
  [[ "${HEAD_NCCL_IB_HCA}" == '=rocep1s0f1:1:0,roceP2p1s0f1:1:0' ]]
  [[ "${WORKER_NCCL_IB_HCA}" == '=rocep1s0f0:1:0,roceP2p1s0f0:1:0' ]]
  [[ "${NCCL_SOCKET_IFNAME}" == '=enP7s7' ]]
  [[ "${TP_SOCKET_IFNAME}" == 'enP7s7' ]]
  [[ "${GLOO_SOCKET_IFNAME}" == 'enP7s7' ]]
  [[ "${MASTER_PORT}" == "29632" ]]
  [[ "${VLLM_PORT}" == "8889" ]]
  [[ "${SERVED_MODEL_NAME}" == "deepseek-v4-flash-dspark-mia-throughput" ]]
  [[ "${SERVED_MODEL_ALIASES}" == "deepseek-v4-flash" ]]
  [[ "${MAX_MODEL_LEN}" == "1048576" ]]
  [[ "${MAX_NUM_SEQS}" == "8" ]]
  [[ "${MAX_NUM_BATCHED_TOKENS}" == "8192" ]]
  [[ "${MAX_CUDAGRAPH_CAPTURE_SIZE}" == "48" ]]
  [[ "${GPU_MEMORY_UTILIZATION}" == "0.78" ]]
  [[ "${DSPARK_MODEL_REPO}" == "apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8" ]]
  [[ "${DSPARK_MODEL_REVISION}" == "7d02640c72a2c8127f116d3d1933ddfec5e4c0fa" ]]
  [[ "${MIA_MODEL_LOCK}" == "MODEL.abliterated-fp8.lock.json" ]]
)
MIA_ENV_FILE="${agent_basename}" \
  "${integration_root}/bin/validate-static.sh" >/dev/null

DSPARK_PROFILE_NAME="${throughput_basename}" \
  "${repo_root}/scripts/configure-dspark-profile.sh" \
    --profile=throughput >/dev/null

(
  unset MASTER_ADDR VLLM_HOST_IP WORKER_VLLM_HOST_IP
  set -a
  # shellcheck disable=SC1090
  source "${throughput_path}"
  set +a
  [[ "${MIA_PROJECT_NAME}" == "mia-dspark-throughput" ]]
  [[ "${HEAD_HOST}" == "cerberus1" ]]
  [[ "${WORKER_HOST}" == "cerberus2" ]]
  [[ -z "${MASTER_ADDR+x}" ]]
  [[ -z "${VLLM_HOST_IP+x}" ]]
  [[ -z "${WORKER_VLLM_HOST_IP+x}" ]]
  [[ "${HEAD_NCCL_IB_HCA}" == '=rocep1s0f1:1:0,roceP2p1s0f1:1:0' ]]
  [[ "${WORKER_NCCL_IB_HCA}" == '=rocep1s0f0:1:0,roceP2p1s0f0:1:0' ]]
  [[ "${NCCL_SOCKET_IFNAME}" == '=enP7s7' ]]
  [[ "${MASTER_PORT}" == "29631" ]]
  [[ "${SERVED_MODEL_ALIASES}" == "deepseek-v4-flash" ]]
  [[ "${MAX_NUM_SEQS}" == "32" ]]
  [[ "${MAX_CUDAGRAPH_CAPTURE_SIZE}" == "192" ]]
)
MIA_ENV_FILE="${throughput_basename}" \
  "${integration_root}/bin/validate-static.sh" >/dev/null

DSPARK_PROFILE_NAME="${official_basename}" \
  "${repo_root}/scripts/configure-dspark-profile.sh" \
    --profile agent --model official >/dev/null
(
  set -a
  # shellcheck disable=SC1090
  source "${official_path}"
  set +a
  [[ "${DSPARK_MODEL_REPO}" == "deepseek-ai/DeepSeek-V4-Flash-DSpark" ]]
  [[ "${DSPARK_MODEL_REVISION}" == "62af8fffb2f7030cac4de2f0169f5b8d1101b646" ]]
  [[ "${MIA_MODEL_LOCK}" == "MODEL.lock.json" ]]
)
MIA_ENV_FILE="${official_basename}" \
  "${integration_root}/bin/validate-static.sh" >/dev/null

for rendered in "${agent_path}" "${throughput_path}" "${official_path}"; do
  grep -Fxq 'HEAD_HOST=cerberus1' "${rendered}"
  grep -Fxq 'WORKER_HOST=cerberus2' "${rendered}"
  if grep -Eq '^(MASTER_ADDR|VLLM_HOST_IP|WORKER_VLLM_HOST_IP)=' "${rendered}"; then
    echo "Rendered profile persisted a runtime management address: ${rendered}" >&2
    exit 1
  fi
done

set +e
deprecated_output="$(
  CERBERUS1_MGMT_IP=192.0.2.99 \
  DSPARK_PROFILE_NAME="${deprecated_basename}" \
    "${repo_root}/scripts/configure-dspark-profile.sh" \
      --profile agent 2>&1
)"
deprecated_status=$?
set -e
[[ "${deprecated_status}" == "2" ]]
grep -Fq 'no longer accepted' <<<"${deprecated_output}"
[[ ! -e "${deprecated_path}" ]]

set +e
invalid_output="$(
  DSPARK_PROFILE_NAME="${invalid_basename}" \
    "${repo_root}/scripts/configure-dspark-profile.sh" \
      --profile invalid 2>&1
)"
invalid_status=$?
set -e
[[ "${invalid_status}" == "2" ]]
grep -Fq "Profile kind must be throughput or agent" <<<"${invalid_output}"

# A pre-ring local profile must fail before the installer can render or place a
# supervisor that would later crash in common.sh.
awk '
  !/^HEAD_NCCL_IB_HCA=/ && !/^WORKER_NCCL_IB_HCA=/ { print }
' "${throughput_path}" >"${invalid_path}"
set +e
stale_output="$(
  MIA_ENV_FILE="${invalid_basename}" \
    "${repo_root}/scripts/install-dspark-supervisor.sh" verify 2>&1
)"
stale_status=$?
set -e
[[ "${stale_status}" == "2" ]]
grep -Fq 'predates rank-specific ring networking' <<<"${stale_output}"

echo "Profile-renderer test passed: scheduler defaults and rank-specific TP2/control networking are statically valid."
