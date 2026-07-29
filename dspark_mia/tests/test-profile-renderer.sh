#!/usr/bin/env bash
set -euo pipefail

integration_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(cd "${integration_root}/.." && pwd -P)"
agent_basename="mia-agent-render-test-${$}.env"
throughput_basename="mia-throughput-render-test-${$}.env"
invalid_basename="mia-invalid-render-test-${$}.env"
agent_path="${integration_root}/${agent_basename}"
throughput_path="${integration_root}/${throughput_basename}"
invalid_path="${integration_root}/${invalid_basename}"

cleanup() {
  rm -f -- "${agent_path}" "${throughput_path}" "${invalid_path}"
}
trap cleanup EXIT

DSPARK_PROFILE_NAME="${agent_basename}" \
  "${repo_root}/scripts/configure-dspark-profile.sh" --profile agent >/dev/null

(
  set -a
  # shellcheck disable=SC1090
  source "${agent_path}"
  set +a
  [[ "${MIA_PROJECT_NAME}" == "mia-dspark-agent" ]]
  [[ "${MASTER_PORT}" == "29632" ]]
  [[ "${VLLM_PORT}" == "8889" ]]
  [[ "${SERVED_MODEL_NAME}" == "deepseek-v4-flash-dspark-mia-throughput" ]]
  [[ "${SERVED_MODEL_ALIASES}" == "deepseek-v4-flash" ]]
  [[ "${MAX_MODEL_LEN}" == "1048576" ]]
  [[ "${MAX_NUM_SEQS}" == "8" ]]
  [[ "${MAX_NUM_BATCHED_TOKENS}" == "8192" ]]
  [[ "${MAX_CUDAGRAPH_CAPTURE_SIZE}" == "48" ]]
  [[ "${GPU_MEMORY_UTILIZATION}" == "0.78" ]]
)
MIA_ENV_FILE="${agent_basename}" \
  "${integration_root}/bin/validate-static.sh" >/dev/null

DSPARK_PROFILE_NAME="${throughput_basename}" \
  "${repo_root}/scripts/configure-dspark-profile.sh" \
    --profile=throughput >/dev/null

(
  set -a
  # shellcheck disable=SC1090
  source "${throughput_path}"
  set +a
  [[ "${MIA_PROJECT_NAME}" == "mia-dspark-throughput" ]]
  [[ "${MASTER_PORT}" == "29631" ]]
  [[ "${SERVED_MODEL_ALIASES}" == "deepseek-v4-flash" ]]
  [[ "${MAX_NUM_SEQS}" == "32" ]]
  [[ "${MAX_CUDAGRAPH_CAPTURE_SIZE}" == "192" ]]
)
MIA_ENV_FILE="${throughput_basename}" \
  "${integration_root}/bin/validate-static.sh" >/dev/null

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

echo "Profile-renderer test passed: throughput/C32 and agent/C8 defaults are isolated and statically valid."
