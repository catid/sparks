#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_EXPERIMENTAL:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_EXPERIMENTAL=1 to run the experimental backend." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
lock="${root}/RUNTIME.lock.json"
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"
backend_port="${AUDIO8_SGLANG_BACKEND_PORT:-18010}"
max_requests="${AUDIO8_SGLANG_MAX_RUNNING_REQUESTS:-2}"
compile="${AUDIO8_SGLANG_ENABLE_TORCH_COMPILE:-1}"
disable_graph="${AUDIO8_SGLANG_DISABLE_CUDA_GRAPH:-0}"
cache_override="${AUDIO8_SGLANG_CACHE_DIR:-}"
reference_dir="${AUDIO8_REFERENCE_DIR:-}"

identity_values="$(
  python3 "${root}/runtime_identity.py" values "${lock}" "${root}"
)"
mapfile -t values <<<"${identity_values}"
[[ "${#values[@]}" == 8 ]] || exit 2
image="${AUDIO8_SGLANG_IMAGE:-${values[3]}}"
model_dir="${AUDIO8_MODEL_DIR:-${HOME}/models/${values[4]}}"
model_revision="${values[5]}"
served_model_name="${values[6]}"
runtime_fingerprint="${values[7]}"
cache_dir="${cache_override:-${HOME}/.cache/cerberus-audio8-sglang/${runtime_fingerprint}}"

if [[ "${backend_port}" != 18010 ]]; then
  echo "Experimental Audio8 SGLang backend port is locked to 18010." >&2
  exit 2
fi
if ! [[ "${max_requests}" =~ ^[1-9][0-9]*$ ]] ||
   ((10#${max_requests} > 8)); then
  echo "AUDIO8_SGLANG_MAX_RUNNING_REQUESTS must be 1-8." >&2
  exit 2
fi
case "${compile}:${disable_graph}" in
  0:0|0:1|1:0|1:1) ;;
  *) echo "Compile and CUDA Graph flags must be 0 or 1." >&2; exit 2 ;;
esac
[[ "${cache_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
  echo "Unsafe Audio8 SGLang cache path: ${cache_dir}" >&2
  exit 2
}
[[ "${model_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
  echo "Unsafe Audio8 model path: ${model_dir}" >&2
  exit 2
}
[[ -d "${model_dir}" && ! -L "${model_dir}" ]] || {
  echo "Missing regular Audio8 model directory: ${model_dir}" >&2
  exit 2
}
[[ -f "${model_dir}/.pinned-revision" && ! -L "${model_dir}/.pinned-revision" &&
   "$(<"${model_dir}/.pinned-revision")" == "${model_revision}" ]] || {
  echo "Audio8 model does not match pinned revision ${model_revision}." >&2
  exit 2
}
[[ "${reference_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
  echo "AUDIO8_REFERENCE_DIR must use a safe canonical absolute path." >&2
  exit 2
}
python3 "${root}/validate_reference.py" \
  "${reference_dir}" "${runtime_uid}" "${runtime_gid}"
image_labels="$(
  docker image inspect --format '{{json .Config.Labels}}' "${image}"
)" || {
  echo "Missing ${image}; run audio8-sglang/build-image.sh first." >&2
  exit 1
}
printf '%s\n' "${image_labels}" | \
  python3 "${root}/runtime_identity.py" verify-labels "${lock}" "${root}"
if ss -ltnH "sport = :${backend_port}" | grep -q .; then
  echo "Backend port ${backend_port} is already in use." >&2
  exit 1
fi

python3 "${root}/prepare_cache.py" \
  "${cache_dir}" "${runtime_fingerprint}" "${runtime_uid}" "${runtime_gid}"
mount_options="$(findmnt -n -o OPTIONS -T "${cache_dir}")"
if [[ ",${mount_options}," == *,noexec,* ]]; then
  echo "Audio8 SGLang cache must permit executable mappings." >&2
  exit 2
fi

exec docker run --rm \
  --name cerberus3-audio8-sglang \
  --user "${runtime_uid}:${runtime_gid}" \
  --gpus all \
  --publish "127.0.0.1:${backend_port}:8010" \
  --ipc private \
  --shm-size 2g \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 2048 \
  --tmpfs "/tmp:rw,exec,nosuid,nodev,size=4g,uid=${runtime_uid},gid=${runtime_gid},mode=1777" \
  --env HOME=/tmp \
  --env USER=audio8 \
  --env LOGNAME=audio8 \
  --env HF_HOME=/cache/huggingface \
  --env CUDA_CACHE_PATH=/cache/cuda \
  --env FLASHINFER_WORKSPACE_BASE=/cache/flashinfer \
  --env TORCHINDUCTOR_CACHE_DIR=/cache/torchinductor \
  --env TRITON_CACHE_DIR=/cache/triton \
  --env MODEL=/models/audio8 \
  --env "MODEL_NAME=${served_model_name}" \
  --env HOST=0.0.0.0 \
  --env PORT=8010 \
  --env AUDIO8_TTS_ATTENTION_BACKEND=flashinfer \
  --env AUDIO8_TTS_MEM_FRACTION_STATIC=0.10 \
  --env "AUDIO8_TTS_MAX_RUNNING_REQUESTS=${max_requests}" \
  --env AUDIO8_TTS_MAX_TOTAL_NUM_TOKENS=8192 \
  --env "AUDIO8_TTS_DISABLE_CUDA_GRAPH=${disable_graph}" \
  --env "AUDIO8_TTS_ENABLE_TORCH_COMPILE=${compile}" \
  --env AUDIO8_TTS_TORCH_COMPILE_MAX_BS=2 \
  --env AUDIO8_TTS_GREEDY_FASTPATH=0 \
  --env AUDIO8_TTS_STREAM_ENABLED=0 \
  --env AUDIO8_TTS_FIXED_REFERENCE_AUDIO=/references/reference.wav \
  --env AUDIO8_TTS_FIXED_REFERENCE_TEXT_FILE=/references/transcript.txt \
  --volume "${cache_dir}:/cache:rw" \
  --volume "${model_dir}:/models/audio8:ro" \
  --volume "${reference_dir}:/references:ro" \
  "${image}" \
  /opt/audio8-tts/sglang_omni/scripts/run_server.sh
