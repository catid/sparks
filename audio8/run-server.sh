#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock="${root}/audio8/MODEL.lock.json"
image="${AUDIO8_IMAGE:-cerberus/audio8-tts:0.6b-f9612f13}"
port="${AUDIO8_PORT:-8010}"
max_active_requests="${AUDIO8_MAX_ACTIVE_REQUESTS:-2}"
sdpa_backend="${AUDIO8_SDPA_BACKEND:-efficient}"
compile_codebooks="${AUDIO8_COMPILE_CODEBOOKS:-0}"
compile_cache_dir="${AUDIO8_COMPILE_CACHE_DIR:-/var/cache/cerberus-audio8/torchinductor}"
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
case "${sdpa_backend}" in
  math|efficient) ;;
  *)
    echo "Invalid AUDIO8_SDPA_BACKEND: ${sdpa_backend}" >&2
    exit 2
    ;;
esac
case "${compile_codebooks}" in
  0|1) ;;
  *)
    echo "Invalid AUDIO8_COMPILE_CODEBOOKS: ${compile_codebooks}" >&2
    exit 2
    ;;
esac
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

compile_args=(--env "AUDIO8_COMPILE_CODEBOOKS=${compile_codebooks}")
if [[ "${compile_codebooks}" == 1 ]]; then
  [[ "${compile_cache_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
    echo "Unsafe AUDIO8_COMPILE_CACHE_DIR: ${compile_cache_dir}" >&2
    exit 2
  }
  install -d -m 0700 -- "${compile_cache_dir}" \
    "${compile_cache_dir}/tmp" "${compile_cache_dir}/triton"
  [[ -d "${compile_cache_dir}" && ! -L "${compile_cache_dir}" &&
     -O "${compile_cache_dir}" && -w "${compile_cache_dir}" &&
     -x "${compile_cache_dir}" ]] || {
    echo "Audio8 compile cache must be an owned writable regular directory." >&2
    exit 2
  }
  mount_options="$(findmnt -n -o OPTIONS -T "${compile_cache_dir}")" || {
    echo "Could not inspect Audio8 compile cache mount." >&2
    exit 2
  }
  if [[ ",${mount_options}," == *,noexec,* ]]; then
    echo "Audio8 compile cache filesystem must allow executable mappings." >&2
    exit 2
  fi
  compile_args+=(
    --env TMPDIR=/compile-cache/tmp
    --env TORCHINDUCTOR_CACHE_DIR=/compile-cache
    --env TRITON_CACHE_DIR=/compile-cache/triton
    --volume "${compile_cache_dir}:/compile-cache:rw"
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
  --env "AUDIO8_SDPA_BACKEND=${sdpa_backend}" \
  "${compile_args[@]}" \
  --env AUDIO8_HOST=0.0.0.0 \
  --env "AUDIO8_PORT=${port}" \
  --volume "${model_dir}:/models/audio8:ro" \
  "${reference_args[@]}" \
  "${image}"
