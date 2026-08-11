#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock="${root}/audio8/MODEL.lock.json"
image="${AUDIO8_IMAGE:-cerberus/audio8-tts:0.6b-f9612f13}"
port="${AUDIO8_PORT:-8010}"
max_active_requests="${AUDIO8_MAX_ACTIVE_REQUESTS:-2}"
reference_dir="${AUDIO8_REFERENCE_DIR:-}"
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"

mapfile -t model_values < <(python3 - "${lock}" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
for key in ("revision", "local_directory", "served_model_name"):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"invalid {key} in model lock")
    print(value)
PY
)
[[ "${#model_values[@]}" == 3 ]] || {
  echo "Could not read Audio8 model lock." >&2
  exit 2
}
revision="${model_values[0]}"
directory="${model_values[1]}"
served_model_name="${model_values[2]}"
model_dir="${AUDIO8_MODEL_DIR:-${HOME}/models/${directory}}"

[[ "${port}" =~ ^[1-9][0-9]{1,4}$ ]] && ((10#${port} <= 65535)) || {
  echo "Invalid AUDIO8_PORT: ${port}" >&2
  exit 2
}
[[ "${max_active_requests}" =~ ^[0-9]+$ ]] &&
  ((10#${max_active_requests} >= 1 && 10#${max_active_requests} <= 32)) || {
  echo "Invalid AUDIO8_MAX_ACTIVE_REQUESTS: ${max_active_requests}" >&2
  exit 2
}
[[ -d "${model_dir}" && ! -L "${model_dir}" ]] || {
  echo "Missing regular Audio8 model directory: ${model_dir}" >&2
  exit 2
}
for required in config.json model.safetensors codec.pth processing_arktts.py; do
  [[ -f "${model_dir}/${required}" && -s "${model_dir}/${required}" &&
     ! -L "${model_dir}/${required}" ]] || {
    echo "Incomplete Audio8 checkpoint: ${model_dir}/${required}" >&2
    exit 2
  }
done
pin_file="${model_dir}/.pinned-revision"
[[ -f "${pin_file}" && ! -L "${pin_file}" && "$(<"${pin_file}")" == "${revision}" ]] || {
  echo "Audio8 checkpoint does not match pinned revision ${revision}." >&2
  exit 2
}

reference_args=()
if [[ -n "${reference_dir}" ]]; then
  [[ -d "${reference_dir}" && ! -L "${reference_dir}" ]] || {
    echo "Audio8 reference directory must be a regular directory: ${reference_dir}" >&2
    exit 2
  }
  for required in reference.wav transcript.txt; do
    [[ -f "${reference_dir}/${required}" && -s "${reference_dir}/${required}" &&
       ! -L "${reference_dir}/${required}" ]] || {
      echo "Incomplete authorized Audio8 reference: ${reference_dir}/${required}" >&2
      exit 2
    }
  done
  reference_args=(
    --env AUDIO8_REFERENCE_AUDIO=/references/reference.wav
    --env AUDIO8_REFERENCE_TEXT_FILE=/references/transcript.txt
    --volume "${reference_dir}:/references:ro"
  )
fi

exec docker run --rm \
  --name cerberus3-audio8 \
  --user "${runtime_uid}:${runtime_gid}" \
  --gpus all \
  --network host \
  --ipc private \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 1024 \
  --tmpfs "/tmp:rw,nosuid,nodev,size=1g,uid=${runtime_uid},gid=${runtime_gid}" \
  --tmpfs "/cache:rw,nosuid,nodev,size=512m,uid=${runtime_uid},gid=${runtime_gid}" \
  --env HOME=/tmp \
  --env USER=audio8 \
  --env LOGNAME=audio8 \
  --env HF_HOME=/cache \
  --env AUDIO8_MODEL_PATH=/models/audio8 \
  --env "AUDIO8_MODEL_NAME=${served_model_name}" \
  --env "AUDIO8_MAX_ACTIVE_REQUESTS=${max_active_requests}" \
  --env AUDIO8_HOST=0.0.0.0 \
  --env "AUDIO8_PORT=${port}" \
  --volume "${model_dir}:/models/audio8:ro" \
  "${reference_args[@]}" \
  "${image}"
