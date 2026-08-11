#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock="${root}/voice_assistant/MODEL.lock.json"
base_image="ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"

mapfile -t model_values < <(python3 - "${lock}" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
for value in (
    data.get("repo_id"),
    data.get("revision"),
    data.get("local_directory"),
    data.get("weights", {}).get("path"),
    data.get("weights", {}).get("sha256"),
    data.get("weights", {}).get("size_bytes"),
):
    if not isinstance(value, (str, int)) or not str(value):
        raise SystemExit("invalid ASR model lock")
    print(value)
PY
)
[[ "${#model_values[@]}" == 6 ]] || {
  echo "Could not read ASR model lock." >&2
  exit 2
}
repo_id="${model_values[0]}"
revision="${model_values[1]}"
directory="${model_values[2]}"
weight_path="${model_values[3]}"
weight_sha256="${model_values[4]}"
weight_size="${model_values[5]}"
model_root="${QWEN_ASR_MODEL_ROOT:-${HOME}/models}"
model_dir="${model_root}/${directory}"

[[ "${model_root}" =~ ^/[A-Za-z0-9._/@+-]+$ && ! -L "${model_root}" ]] || {
  echo "Unsafe ASR model root: ${model_root}" >&2
  exit 2
}
[[ ! -L "${model_dir}" ]] || {
  echo "ASR model directory cannot be a symlink: ${model_dir}" >&2
  exit 2
}
install -d -m 0755 -- "${model_root}" "${model_dir}"
docker image inspect "${base_image}" >/dev/null

docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env HF_HOME=/tmp/huggingface \
  --volume "${model_dir}:/model" \
  --entrypoint /usr/bin/python3 \
  "${base_image}" \
  -c 'from huggingface_hub import snapshot_download; import sys; snapshot_download(repo_id=sys.argv[1], revision=sys.argv[2], local_dir="/model")' \
  "${repo_id}" "${revision}"

for required in \
  config.json generation_config.json processor_config.json \
  tokenizer.json tokenizer_config.json chat_template.jinja "${weight_path}"; do
  [[ -f "${model_dir}/${required}" && -s "${model_dir}/${required}" &&
     ! -L "${model_dir}/${required}" ]] || {
    echo "Downloaded ASR checkpoint is incomplete: ${model_dir}/${required}" >&2
    exit 1
  }
done
actual_size="$(stat -c '%s' -- "${model_dir}/${weight_path}")"
[[ "${actual_size}" == "${weight_size}" ]] || {
  echo "ASR weight size mismatch: expected ${weight_size}, got ${actual_size}" >&2
  exit 1
}
actual_sha256="$(sha256sum -- "${model_dir}/${weight_path}" | cut -d' ' -f1)"
[[ "${actual_sha256}" == "${weight_sha256}" ]] || {
  echo "ASR weight SHA-256 mismatch." >&2
  exit 1
}

pin_temporary="$(mktemp "${model_dir}/.pinned-revision.tmp.XXXXXX")"
cleanup_pin() { rm -f -- "${pin_temporary}"; }
trap cleanup_pin EXIT
printf '%s\n' "${revision}" >"${pin_temporary}"
chmod 0644 "${pin_temporary}"
mv -fT -- "${pin_temporary}" "${model_dir}/.pinned-revision"
trap - EXIT
printf '%s\n' "${model_dir}"
