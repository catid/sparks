#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command jq
need_command sha256sum

lock_schema="$(jq -er '.schema' "${MIA3_MODEL_LOCK}")"
lock_repo="$(jq -er '.repo_id' "${MIA3_MODEL_LOCK}")"
lock_revision="$(jq -er '.revision' "${MIA3_MODEL_LOCK}")"
lock_container_path="$(jq -er '.container_path' "${MIA3_MODEL_LOCK}")"
expected_shards="$(jq -er '.expected_safetensor_shards' "${MIA3_MODEL_LOCK}")"
expected_bytes="$(jq -er '.expected_safetensor_bytes' "${MIA3_MODEL_LOCK}")"

[[ "${lock_schema}" == 2 ]] || { echo "Unsupported model lock schema: ${lock_schema}" >&2; exit 1; }
[[ "${DSPARK_MODEL_REPO}" == "${lock_repo}" ]] || { echo "Model repository differs from lock." >&2; exit 1; }
[[ "${DSPARK_MODEL_REVISION}" == "${lock_revision}" ]] || { echo "Model revision differs from lock." >&2; exit 1; }
[[ "${DSPARK_MODEL}" == "${lock_container_path}" ]] || { echo "Container model path differs from lock." >&2; exit 1; }
[[ -d "${DSPARK_MODEL_HOST_PATH}" && ! -L "${DSPARK_MODEL_HOST_PATH}" ]] || {
  echo "Pinned model directory is absent or a symlink: ${DSPARK_MODEL_HOST_PATH}" >&2
  exit 1
}

for relative in config.json model.safetensors.index.json; do
  file="${DSPARK_MODEL_HOST_PATH}/${relative}"
  [[ -f "${file}" && ! -L "${file}" ]] || { echo "Missing regular model file: ${file}" >&2; exit 1; }
  expected_file_bytes="$(jq -er --arg name "${relative}" '.key_files[$name].bytes' "${MIA3_MODEL_LOCK}")"
  expected_sha="$(jq -er --arg name "${relative}" '.key_files[$name].sha256' "${MIA3_MODEL_LOCK}")"
  actual_file_bytes="$(stat -c '%s' "${file}")"
  actual_sha="$(sha256sum "${file}" | awk '{print $1}')"
  [[ "${actual_file_bytes}" == "${expected_file_bytes}" ]] || {
    echo "${relative}: bytes=${actual_file_bytes}, expected=${expected_file_bytes}" >&2
    exit 1
  }
  [[ "${actual_sha}" == "${expected_sha}" ]] || {
    echo "${relative}: sha256=${actual_sha}, expected=${expected_sha}" >&2
    exit 1
  }
done

metadata="${DSPARK_MODEL_HOST_PATH}/.cache/huggingface/download/config.json.metadata"
[[ -f "${metadata}" ]] || { echo "Missing Hugging Face revision metadata: ${metadata}" >&2; exit 1; }
read -r metadata_revision <"${metadata}"
[[ "${metadata_revision}" == "${lock_revision}" ]] || {
  echo "Local model revision=${metadata_revision}, expected=${lock_revision}" >&2
  exit 1
}

mapfile -t index_shards < <(
  jq -r '.weight_map | values[]' "${DSPARK_MODEL_HOST_PATH}/model.safetensors.index.json" | sort -u
)
[[ "${#index_shards[@]}" == "${expected_shards}" ]] || {
  echo "Index has ${#index_shards[@]} shards, expected=${expected_shards}" >&2
  exit 1
}

actual_bytes=0
for shard in "${index_shards[@]}"; do
  shard_path="${DSPARK_MODEL_HOST_PATH}/${shard}"
  [[ -f "${shard_path}" && ! -L "${shard_path}" ]] || {
    echo "Missing regular checkpoint shard: ${shard_path}" >&2
    exit 1
  }
  ((actual_bytes += $(stat -c '%s' "${shard_path}")))
done
[[ "${actual_bytes}" == "${expected_bytes}" ]] || {
  echo "Safetensor bytes=${actual_bytes}, expected=${expected_bytes}" >&2
  exit 1
}

jq -e --argjson layers "${MODEL_NUM_LAYERS}" \
  --argjson heads "${MODEL_NUM_ATTENTION_HEADS}" \
  --argjson experts "${MODEL_NUM_ROUTED_EXPERTS}" '
  .model_type == "deepseek_v4" and
  .dspark_block_size == 5 and
  .max_position_embeddings == 1048576 and
  .num_hidden_layers == $layers and
  .num_attention_heads == $heads and
  .n_routed_experts == $experts and
  .quantization_config.quant_method == "fp8"
' "${DSPARK_MODEL_HOST_PATH}/config.json" >/dev/null || {
  echo "Pinned config does not match the expected 43-layer FP8 DS4F checkpoint." >&2
  exit 1
}

echo "Pinned model complete: ${lock_repo}@${lock_revision}"
echo "path=${DSPARK_MODEL_HOST_PATH} shards=${expected_shards} bytes=${actual_bytes}"
