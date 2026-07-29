#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command docker
need_command git
need_command jq

expected_capture_size="$((MAX_NUM_SEQS * (MTP_NUM_TOKENS + 1)))"
[[ "${MAX_CUDAGRAPH_CAPTURE_SIZE}" == "${expected_capture_size}" ]] || {
  echo "MAX_CUDAGRAPH_CAPTURE_SIZE=${MAX_CUDAGRAPH_CAPTURE_SIZE}, expected=${expected_capture_size} (seqs*(k+1))." >&2
  exit 1
}

expected_commit="$(awk -F= '$1 == "commit" {print $2}' "${MIA_UPSTREAM_LOCK}")"
expected_tree="$(awk -F= '$1 == "tree" {print $2}' "${MIA_UPSTREAM_LOCK}")"
expected_repository="$(awk -F= '$1 == "repository" {sub(/^repository=/, ""); print}' "${MIA_UPSTREAM_LOCK}")"
expected_image="$(awk -F= '$1 == "image" {sub(/^image=/, ""); print}' "${MIA_UPSTREAM_LOCK}")"
actual_commit="$(git -C "${MIA_ROOT}/upstream" rev-parse HEAD)"
actual_tree="$(git -C "${MIA_ROOT}/upstream" rev-parse 'HEAD^{tree}')"
actual_repository="$(git -C "${MIA_ROOT}/upstream" remote get-url origin)"

[[ "${actual_repository}" == "${expected_repository}" ]] || {
  echo "Upstream origin=${actual_repository}, expected=${expected_repository}" >&2
  exit 1
}
[[ "${actual_commit}" == "${expected_commit}" ]] || {
  echo "Upstream commit=${actual_commit}, expected=${expected_commit}" >&2
  exit 1
}
[[ "${actual_tree}" == "${expected_tree}" ]] || {
  echo "Upstream tree=${actual_tree}, expected=${expected_tree}" >&2
  exit 1
}
[[ -z "$(git -C "${MIA_ROOT}/upstream" status --porcelain)" ]] || {
  echo "Pinned upstream checkout has local changes." >&2
  exit 1
}
[[ "${DSPARK_VLLM_IMAGE}" == "${expected_image}" ]] || {
  echo "Configured image is not the UPSTREAM.lock digest." >&2
  exit 1
}

for script in "${MIA_ROOT}"/bin/*.sh; do
  bash -n "${script}"
done

rank0_json="$(mktemp)"
rank1_json="$(mktemp)"
cleanup() {
  rm -f -- "${rank0_json}" "${rank1_json}"
}
trap cleanup EXIT

"${MIA_ROOT}/bin/node-compose.sh" 0 config --format json >"${rank0_json}"
"${MIA_ROOT}/bin/node-compose.sh" 1 config --format json >"${rank1_json}"

for rendered in "${rank0_json}" "${rank1_json}"; do
  jq -e --arg project "${MIA_PROJECT_NAME}" \
    '.name == $project' "${rendered}" >/dev/null
  jq -e --arg image "${DSPARK_VLLM_IMAGE}" \
    '.services["vllm-dspark"].image == $image' "${rendered}" >/dev/null
  jq -e '.services["vllm-dspark"].pull_policy == "never"' "${rendered}" >/dev/null
  jq -e '.services["vllm-dspark"].restart == "no"' "${rendered}" >/dev/null
  jq -e '
    .services["vllm-dspark"].ulimits.nofile.soft == 500000 and
    .services["vllm-dspark"].ulimits.nofile.hard == 500000 and
    .services["vllm-dspark"].ulimits.memlock == -1
  ' "${rendered}" >/dev/null
  jq -e --arg source "${DSPARK_MODEL_HOST_PATH}" --arg target "${DSPARK_MODEL}" '
    any(.services["vllm-dspark"].volumes[];
      .type == "bind" and .source == $source and .target == $target and .read_only == true)
  ' "${rendered}" >/dev/null
  jq -e --arg hca "${NCCL_IB_HCA}" '
    .services["vllm-dspark"].environment as $e |
    $e.NCCL_IB_HCA == $hca and
    $e.NCCL_NETDEVS_POLICY == "ALL" and
    $e.NCCL_CROSS_NIC == "0" and
    $e.NCCL_IB_MERGE_NICS == "0" and
    (((($e | has("NCCL_IB_GID_INDEX")) | not) or
      $e.NCCL_IB_GID_INDEX == null)) and
    $e.MTP_NUM_TOKENS == "5"
  ' "${rendered}" >/dev/null
  jq -e \
    --arg port "${VLLM_PORT}" \
    --arg master_port "${MASTER_PORT}" \
    --arg max_model_len "${MAX_MODEL_LEN}" \
    --arg max_num_seqs "${MAX_NUM_SEQS}" \
    --arg max_batched_tokens "${MAX_NUM_BATCHED_TOKENS}" \
    --arg capture_size "${MAX_CUDAGRAPH_CAPTURE_SIZE}" \
    --arg gpu_util "${GPU_MEMORY_UTILIZATION}" \
    '
    (.services["vllm-dspark"].command | join(" ")) as $c |
    ($c | contains("--tensor-parallel-size 2")) and
    ($c | contains("--pipeline-parallel-size 1")) and
    ($c | contains("--port " + $port)) and
    ($c | contains("--master-port " + $master_port)) and
    ($c | contains("--gpu-memory-utilization " + $gpu_util)) and
    ($c | contains("--max-model-len " + $max_model_len)) and
    ($c | contains("--max-num-seqs " + $max_num_seqs)) and
    ($c | contains("--max-num-batched-tokens " + $max_batched_tokens)) and
    ($c | contains("--max-cudagraph-capture-size " + $capture_size)) and
    ($c | contains("--kv-cache-dtype nvfp4_ds_mla")) and
    ($c | contains("method")) and
    ($c | contains("dspark")) and
    ($c | contains("num_speculative_tokens")) and
    ($c | contains("draft_sample_method")) and
    ($c | contains("probabilistic")) and
    ($c | contains("{\"thinking\":true}")) and
    (($c | contains("--port 8000")) | not) and
    (($c | contains("--master-port 29601")) | not)
  ' "${rendered}" >/dev/null

  while IFS= read -r model_id; do
    jq -e --arg model_id "${model_id}" '
      (.services["vllm-dspark"].command | join(" ")) |
      contains(" " + $model_id + " ")
    ' "${rendered}" >/dev/null
  done < <(served_model_ids)
done

jq -e '
  .services["vllm-dspark"].environment.NODE_RANK == "0" and
  .services["vllm-dspark"].environment.VLLM_HOST_IP == "192.168.100.10" and
  ((.services["vllm-dspark"].command | join(" ") | contains("--headless")) | not)
' "${rank0_json}" >/dev/null
jq -e '
  .services["vllm-dspark"].environment.NODE_RANK == "1" and
  .services["vllm-dspark"].environment.VLLM_HOST_IP == "192.168.100.11" and
  (.services["vllm-dspark"].command | join(" ") | contains("--headless"))
' "${rank1_json}" >/dev/null

echo "Static validation passed."
echo "profile_file=${MIA_ENV_BASENAME} project=${MIA_PROJECT_NAME}"
echo "upstream=${expected_commit} tree=${expected_tree}"
echo "image=${DSPARK_VLLM_IMAGE}"
echo "profile=TP2/PP1 DSpark-k5 thinking=true API=${VLLM_PORT} master=${MASTER_PORT} four-rail"
echo "limits=max_model_len=${MAX_MODEL_LEN} max_num_seqs=${MAX_NUM_SEQS} max_batched_tokens=${MAX_NUM_BATCHED_TOKENS} capture=${MAX_CUDAGRAPH_CAPTURE_SIZE} gpu_util=${GPU_MEMORY_UTILIZATION}"
