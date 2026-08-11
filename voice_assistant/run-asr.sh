#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock="${root}/voice_assistant/MODEL.lock.json"
image="${QWEN_ASR_IMAGE:-cerberus/qwen3-asr:1.7b-bcd2b5b7}"
port="${QWEN_ASR_PORT:-8020}"
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"

mapfile -t model_values < <(python3 - "${lock}" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
for value in (
    data.get("revision"),
    data.get("local_directory"),
    data.get("served_model_name"),
    data.get("weights", {}).get("path"),
    data.get("weights", {}).get("size_bytes"),
):
    if not isinstance(value, (str, int)) or not str(value):
        raise SystemExit("invalid ASR model lock")
    print(value)
PY
)
[[ "${#model_values[@]}" == 5 ]] || {
  echo "Could not read ASR model lock." >&2
  exit 2
}
revision="${model_values[0]}"
directory="${model_values[1]}"
served_model_name="${model_values[2]}"
weight_path="${model_values[3]}"
weight_size="${model_values[4]}"
model_dir="${QWEN_ASR_MODEL_DIR:-${HOME}/models/${directory}}"

[[ "${port}" =~ ^[1-9][0-9]{1,4}$ ]] && ((10#${port} <= 65535)) || {
  echo "Invalid QWEN_ASR_PORT: ${port}" >&2
  exit 2
}
[[ -d "${model_dir}" && ! -L "${model_dir}" ]] || {
  echo "Missing regular ASR model directory: ${model_dir}" >&2
  exit 2
}
for required in \
  config.json processor_config.json tokenizer.json chat_template.jinja "${weight_path}"; do
  [[ -f "${model_dir}/${required}" && -s "${model_dir}/${required}" &&
     ! -L "${model_dir}/${required}" ]] || {
    echo "Incomplete ASR checkpoint: ${model_dir}/${required}" >&2
    exit 2
  }
done
[[ "$(stat -c '%s' -- "${model_dir}/${weight_path}")" == "${weight_size}" ]] || {
  echo "ASR weight size does not match the model lock." >&2
  exit 2
}
pin_file="${model_dir}/.pinned-revision"
[[ -f "${pin_file}" && ! -L "${pin_file}" && "$(<"${pin_file}")" == "${revision}" ]] || {
  echo "ASR checkpoint does not match pinned revision ${revision}." >&2
  exit 2
}

exec docker run --rm \
  --name cerberus3-qwen-asr \
  --user "${runtime_uid}:${runtime_gid}" \
  --gpus all \
  --network host \
  --ipc private \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 1024 \
  --stop-timeout 30 \
  --tmpfs "/tmp:rw,nosuid,nodev,size=1g,uid=${runtime_uid},gid=${runtime_gid}" \
  --tmpfs "/cache:rw,nosuid,nodev,size=1g,uid=${runtime_uid},gid=${runtime_gid}" \
  --env HOME=/tmp \
  --env USER=qwen-asr \
  --env LOGNAME=qwen-asr \
  --env HF_HOME=/cache \
  --env TRANSFORMERS_OFFLINE=1 \
  --env HF_HUB_OFFLINE=1 \
  --env QWEN_ASR_MODEL_PATH=/models/qwen-asr \
  --env "QWEN_ASR_MODEL_NAME=${served_model_name}" \
  --env QWEN_ASR_HOST=127.0.0.1 \
  --env "QWEN_ASR_PORT=${port}" \
  --env "QWEN_ASR_LANGUAGE=${QWEN_ASR_LANGUAGE:-en}" \
  --env "QWEN_ASR_VOCABULARY_PROMPT=${QWEN_ASR_VOCABULARY_PROMPT:-Vocabulary: Cerberus, Cerberus One, Cerberus Two, Cerberus Three, cerberus1, cerberus2, cerberus3.}" \
  --env "QWEN_ASR_MAX_AUDIO_SECONDS=${QWEN_ASR_MAX_AUDIO_SECONDS:-35}" \
  --env "QWEN_ASR_MAX_NEW_TOKENS=${QWEN_ASR_MAX_NEW_TOKENS:-256}" \
  --volume "${model_dir}:/models/qwen-asr:ro" \
  "${image}"
