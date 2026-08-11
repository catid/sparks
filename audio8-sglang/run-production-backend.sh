#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_PRODUCTION:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_PRODUCTION=1 to run the production backend." >&2
  exit 2
}
[[ "${AUDIO8_SGLANG_EXPERIMENTAL:-0}" != 1 ]] || {
  echo "Production and experimental modes are mutually exclusive." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
lock="${root}/RUNTIME.lock.json"
runtime_uid="$(id -u)"
runtime_gid="$(id -g)"
reference_dir="${AUDIO8_REFERENCE_DIR:-}"
backend_network=cerberus3-audio8-sglang-backend
backend_ip=172.30.82.2

identity_values="$(python3 "${root}/runtime_identity.py" values "${lock}" "${root}")"
mapfile -t values <<<"${identity_values}"
[[ "${#values[@]}" == 8 ]] || exit 2
image="${values[3]}"
model_dir="${AUDIO8_MODEL_DIR:-${HOME}/models/${values[4]}}"
model_revision="${values[5]}"
served_model_name="${values[6]}"
runtime_fingerprint="${values[7]}"
cache_dir="${HOME}/.cache/cerberus-audio8-sglang/${runtime_fingerprint}"

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

network_contract="$(docker network inspect --format \
  '{{.Driver}}|{{.Scope}}|{{.Internal}}|{{(index .IPAM.Config 0).Subnet}}|{{index .Labels "io.cerberus.audio8-sglang.role"}}' \
  "${backend_network}")" || {
  echo "Missing verified production backend network." >&2
  exit 1
}
[[ "${network_contract}" == 'bridge|local|true|172.30.82.0/29|backend' ]] || {
  echo "Production backend network contract is invalid." >&2
  exit 1
}

image_labels="$(docker image inspect --format '{{json .Config.Labels}}' "${image}")" || {
  echo "Missing ${image}; run audio8-sglang/build-image.sh first." >&2
  exit 1
}
printf '%s\n' "${image_labels}" | \
  python3 "${root}/runtime_identity.py" verify-labels "${lock}" "${root}"

python3 "${root}/prepare_cache.py" \
  "${cache_dir}" "${runtime_fingerprint}" "${runtime_uid}" "${runtime_gid}"
mount_options="$(findmnt -n -o OPTIONS -T "${cache_dir}")"
[[ ",${mount_options}," != *,noexec,* ]] || {
  echo "Audio8 SGLang cache must permit executable mappings." >&2
  exit 2
}

exec docker run --rm --pull never \
  --name cerberus3-audio8-sglang-backend \
  --label io.cerberus.audio8-sglang.role=backend \
  --label "io.cerberus.audio8-sglang.runtime-fingerprint=${runtime_fingerprint}" \
  --user "${runtime_uid}:${runtime_gid}" \
  --gpus device=0 \
  --network "name=${backend_network},ip=${backend_ip}" \
  --network-alias audio8-sglang-backend \
  --ipc private \
  --shm-size 2g \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 2048 \
  --tmpfs "/tmp:rw,exec,nosuid,nodev,size=4g,uid=${runtime_uid},gid=${runtime_gid},mode=1777" \
  --health-cmd 'python3 /opt/cerberus/check_health.py backend' \
  --health-interval 5s \
  --health-timeout 4s \
  --health-start-period 240s \
  --health-retries 3 \
  --env HOME=/tmp \
  --env USER=audio8 \
  --env LOGNAME=audio8 \
  --env HF_HOME=/cache/huggingface \
  --env HF_HUB_OFFLINE=1 \
  --env TRANSFORMERS_OFFLINE=1 \
  --env HTTP_PROXY= \
  --env HTTPS_PROXY= \
  --env ALL_PROXY= \
  --env http_proxy= \
  --env https_proxy= \
  --env all_proxy= \
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
  --env AUDIO8_TTS_MAX_RUNNING_REQUESTS=2 \
  --env AUDIO8_TTS_MAX_TOTAL_NUM_TOKENS=8192 \
  --env AUDIO8_TTS_DISABLE_CUDA_GRAPH=0 \
  --env AUDIO8_TTS_ENABLE_TORCH_COMPILE=1 \
  --env AUDIO8_TTS_TORCH_COMPILE_MAX_BS=2 \
  --env AUDIO8_TTS_GREEDY_FASTPATH=0 \
  --env AUDIO8_TTS_STREAM_ENABLED=0 \
  --env AUDIO8_TTS_FIXED_REFERENCE_AUDIO=/references/reference.wav \
  --env AUDIO8_TTS_FIXED_REFERENCE_TEXT_FILE=/references/transcript.txt \
  --volume "${cache_dir}:/cache:rw" \
  --volume "${model_dir}:/models/audio8:ro" \
  --volume "${reference_dir}:/references:ro" \
  --volume "${root}/gateway.py:/opt/cerberus/gateway.py:ro" \
  --volume "${root}/check_health.py:/opt/cerberus/check_health.py:ro" \
  "${image}" \
  /opt/audio8-tts/sglang_omni/scripts/run_server.sh
