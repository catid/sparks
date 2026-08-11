#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command jq
need_command sudo

"${MIA3_ROOT}/bin/validate-parallelism.sh" \
  "${TP_SIZE}" "${PP_SIZE}" "${NNODES}" \
  "${MODEL_NUM_ATTENTION_HEADS}" "${MODEL_NUM_ROUTED_EXPERTS}" \
  "${MODEL_NUM_LAYERS}" "${VLLM_PP_LAYER_PARTITION}"

expected_image="$(awk -F= '$1 == "image" {sub(/^image=/, ""); print}' "${MIA3_UPSTREAM_LOCK}")"
expected_repo="$(awk -F= '$1 == "model_repo" {sub(/^model_repo=/, ""); print}' "${MIA3_UPSTREAM_LOCK}")"
expected_revision="$(awk -F= '$1 == "model_revision" {sub(/^model_revision=/, ""); print}' "${MIA3_UPSTREAM_LOCK}")"
[[ "${DSPARK_VLLM_IMAGE}" == "${expected_image}" ]] || { echo "Image differs from UPSTREAM.lock." >&2; exit 1; }
[[ "${DSPARK_VLLM_IMAGE}" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] || { echo "Image must use an immutable sha256 digest." >&2; exit 1; }
[[ "${DSPARK_MODEL_REPO}" == "${expected_repo}" ]] || { echo "Model repo differs from UPSTREAM.lock." >&2; exit 1; }
[[ "${DSPARK_MODEL_REVISION}" == "${expected_revision}" ]] || { echo "Model revision differs from UPSTREAM.lock." >&2; exit 1; }

[[ "${HEAD_HOST}" == "cerberus1" ]] || { echo "Rank 0 must be cerberus1." >&2; exit 1; }
[[ "${RANK1_HOST}" == "cerberus2" ]] || { echo "Rank 1 must be cerberus2." >&2; exit 1; }
[[ "${RANK2_HOST}" == "cerberus3" ]] || { echo "Rank 2 must be cerberus3." >&2; exit 1; }
[[ "${MIA_PROJECT_NAME}" == "mia-dspark-pp3-trial" ]] || { echo "Trial must keep its isolated Compose project." >&2; exit 1; }
case "${VLLM_PORT}" in 8000|8888|8889) echo "Trial API port collides with an existing profile." >&2; exit 1;; esac
case "${MASTER_PORT}" in 25000|29601|29630|29631|29632) echo "Trial rendezvous port collides with an existing profile." >&2; exit 1;; esac
[[ "${NCCL_IB_SUBNET_AWARE_ROUTING}" == 1 ]] || { echo "Ring routing must be subnet-aware." >&2; exit 1; }
[[ "${NCCL_NET_PLUGIN}" == none ]] || { echo "Ring trial must use the internal NCCL network plugin." >&2; exit 1; }
[[ "${CX7_C3_PORT_MAP}" == c3-p0-to-c2 ]] || {
  echo "Three-node trial requires the crossed C3 P0-to-C2 port map." >&2
  exit 1
}
[[ "${NCCL_IB_HCA}" == '=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1' ]] || {
  echo "All four RoCE HCAs must be selected in the trial." >&2
  exit 1
}

resolve_management_plane
master_addr="$(rank_runtime_ipv4 0)"

while IFS= read -r -d '' script; do
  bash -n "${script}"
done < <(find "${MIA3_ROOT}/bin" "${MIA3_ROOT}/tests" -type f -name '*.sh' -print0 2>/dev/null)

render_dir="$(mktemp -d)"
cleanup() { rm -rf -- "${render_dir}"; }
trap cleanup EXIT

for rank in 0 1 2; do
  MIA3_RENDER_LAUNCH_CONFIG=1 \
    "${MIA3_ROOT}/bin/node-compose.sh" "${rank}" config --format json \
      >"${render_dir}/rank${rank}.json"
done

for rank in 0 1 2; do
  rendered="${render_dir}/rank${rank}.json"
  expected_ip="$(rank_runtime_ipv4 "${rank}")"
  expected_headless="$(rank_headless "${rank}")"
  jq -e --arg project "${MIA_PROJECT_NAME}" --arg image "${DSPARK_VLLM_IMAGE}" \
    --arg model_source "${DSPARK_MODEL_HOST_PATH}" --arg model_target "${DSPARK_MODEL}" \
    --arg rank "${rank}" --arg ip "${expected_ip}" --arg headless "${expected_headless}" \
    --arg partition "${VLLM_PP_LAYER_PARTITION}" --arg dspark "${ENABLE_DSPARK}" \
    --arg tp "${TP_SIZE}" --arg pp "${PP_SIZE}" --arg nnodes "${NNODES}" \
    --arg master_addr "${master_addr}" --arg master_port "${MASTER_PORT}" \
    --arg api_port "${VLLM_PORT}" '
      .name == $project and
      .services["vllm-dspark"].image == $image and
      .services["vllm-dspark"].pull_policy == "never" and
      .services["vllm-dspark"].restart == "no" and
      .services["vllm-dspark"].environment.NODE_RANK == $rank and
      .services["vllm-dspark"].environment.VLLM_HOST_IP == $ip and
      .services["vllm-dspark"].environment.HEADLESS == $headless and
      .services["vllm-dspark"].environment.VLLM_PP_LAYER_PARTITION == $partition and
      .services["vllm-dspark"].environment.ENABLE_DSPARK == $dspark and
      .services["vllm-dspark"].environment.TP_SIZE == $tp and
      .services["vllm-dspark"].environment.PP_SIZE == $pp and
      .services["vllm-dspark"].environment.NNODES == $nnodes and
      .services["vllm-dspark"].environment.MASTER_ADDR == $master_addr and
      .services["vllm-dspark"].environment.MASTER_PORT == $master_port and
      .services["vllm-dspark"].environment.VLLM_PORT == $api_port and
      .services["vllm-dspark"].environment.NCCL_IB_SUBNET_AWARE_ROUTING == "1" and
      .services["vllm-dspark"].environment.NCCL_NET_PLUGIN == "none" and
      ((.services["vllm-dspark"].environment | has("NCCL_NETDEVS_POLICY")) | not) and
      (((.services["vllm-dspark"].environment | has("NCCL_IB_GID_INDEX")) | not) or
        .services["vllm-dspark"].environment.NCCL_IB_GID_INDEX == null) and
      any(.services["vllm-dspark"].volumes[];
        .type == "bind" and .source == $model_source and .target == $model_target and .read_only == true)
    ' "${rendered}" >/dev/null

  jq -e --arg api "${VLLM_PORT}" --arg master "${MASTER_PORT}" '
    (.services["vllm-dspark"].command | join(" ")) as $c |
    ($c | contains("--tensor-parallel-size")) and
    ($c | contains("--pipeline-parallel-size")) and
    ($c | contains("--distributed-executor-backend mp")) and
    ($c | contains("--distributed-timeout-seconds 1800")) and
    ($c | contains("--nnodes")) and
    ($c | contains("--port")) and
    ($c | contains("--master-port")) and
    ($c | contains("thinking")) and
    (($c | contains("--port 8889")) | not) and
    (($c | contains("--master-port 29632")) | not)
  ' "${rendered}" >/dev/null
done

if command -v rg >/dev/null 2>&1 && rg -n \
  '(sk-(proj|ant|or)-|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-|hf_[0-9A-Za-z]{20,})' \
  "${MIA3_ROOT}" >/dev/null; then
  echo "Possible credential found in dspark_mia3; refusing validation." >&2
  exit 1
fi

echo "Static validation passed: TP1/PP3, profile=${MIA3_PARTITION_PROFILE}, DFlash=${ENABLE_DSPARK}."
echo "project=${MIA_PROJECT_NAME} API=${VLLM_PORT} master=${MIA3_RUNTIME_MGMT_NAMES[0]}:${MASTER_PORT}"
