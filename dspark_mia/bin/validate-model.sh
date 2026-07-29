#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command jq
need_command sha256sum

lock_schema="$(jq -er '.schema' "${MIA_MODEL_LOCK}")"
lock_repo="$(jq -er '.repo_id' "${MIA_MODEL_LOCK}")"
lock_revision="$(jq -er '.revision' "${MIA_MODEL_LOCK}")"
lock_path="${DSPARK_MODEL_HOST_PATH}"
default_lock_path="$(jq -er '.default_host_path' "${MIA_MODEL_LOCK}")"
lock_container_path="$(jq -er '.container_path' "${MIA_MODEL_LOCK}")"
expected_shards="$(jq -er '.expected_safetensor_shards' "${MIA_MODEL_LOCK}")"
expected_bytes="$(jq -er '.expected_safetensor_bytes' "${MIA_MODEL_LOCK}")"

[[ "${lock_schema}" == "2" ]] || {
  echo "Unsupported MODEL.lock.json schema: ${lock_schema}" >&2
  exit 1
}
[[ "${DSPARK_MODEL_REPO}" == "${lock_repo}" ]] || {
  echo "Model repo differs from MODEL.lock.json." >&2
  exit 1
}
[[ "${DSPARK_MODEL_REVISION}" == "${lock_revision}" ]] || {
  echo "Model revision differs from MODEL.lock.json." >&2
  exit 1
}
[[ "${DSPARK_MODEL}" == "${lock_container_path}" ]] || {
  echo "Container model path differs from MODEL.lock.json." >&2
  exit 1
}
[[ -d "${lock_path}" ]] || {
  echo "Pinned model directory is absent: ${lock_path}" >&2
  exit 1
}

for relative in config.json model.safetensors.index.json; do
  file="${lock_path}/${relative}"
  [[ -f "${file}" && ! -L "${file}" ]] || {
    echo "Missing regular pinned model file: ${file}" >&2
    exit 1
  }
  expected_file_bytes="$(jq -er --arg name "${relative}" '.key_files[$name].bytes' "${MIA_MODEL_LOCK}")"
  expected_sha="$(jq -er --arg name "${relative}" '.key_files[$name].sha256' "${MIA_MODEL_LOCK}")"
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

metadata="${lock_path}/.cache/huggingface/download/config.json.metadata"
[[ -f "${metadata}" ]] || {
  echo "Missing Hugging Face revision metadata: ${metadata}" >&2
  exit 1
}
read -r metadata_revision <"${metadata}"
[[ "${metadata_revision}" == "${lock_revision}" ]] || {
  echo "Local HF revision=${metadata_revision}, expected=${lock_revision}" >&2
  exit 1
}

mapfile -t index_shards < <(
  jq -r '.weight_map | values[]' "${lock_path}/model.safetensors.index.json" |
    sort -u
)
[[ "${#index_shards[@]}" == "${expected_shards}" ]] || {
  echo "Index has ${#index_shards[@]} shards, expected=${expected_shards}" >&2
  exit 1
}

actual_bytes=0
for shard in "${index_shards[@]}"; do
  shard_path="${lock_path}/${shard}"
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

jq -e '
  .dspark_block_size == 5 and
  .max_position_embeddings == 1048576 and
  .model_type == "deepseek_v4"
' "${lock_path}/config.json" >/dev/null || {
  echo "Pinned config is not the expected 1M DeepSeek V4 DSpark checkpoint." >&2
  exit 1
}

echo "Pinned model is complete: ${lock_repo}@${lock_revision}"
echo "path=${lock_path} shards=${expected_shards} safetensor_bytes=${actual_bytes}"
echo "lock_default_path=${default_lock_path}"
