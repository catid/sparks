#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
lock="${root}/audio8/MODEL.lock.json"
base_image="ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8"

mapfile -t model_values < <(python3 - "${lock}" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
for key in ("repo_id", "revision", "local_directory"):
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
repo_id="${model_values[0]}"
revision="${model_values[1]}"
directory="${model_values[2]}"
model_root="${AUDIO8_MODEL_ROOT:-${HOME}/models}"
model_dir="${model_root}/${directory}"

[[ "${model_root}" =~ ^/[A-Za-z0-9._/@+-]+$ && ! -L "${model_root}" ]] || {
  echo "Unsafe Audio8 model root: ${model_root}" >&2
  exit 2
}
[[ ! -L "${model_dir}" ]] || {
  echo "Audio8 model directory cannot be a symlink: ${model_dir}" >&2
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

for required in config.json model.safetensors codec.pth processing_arktts.py; do
  [[ -f "${model_dir}/${required}" && -s "${model_dir}/${required}" &&
     ! -L "${model_dir}/${required}" ]] || {
    echo "Downloaded checkpoint is incomplete: ${model_dir}/${required}" >&2
    exit 1
  }
done
pin_temporary="$(mktemp "${model_dir}/.pinned-revision.tmp.XXXXXX")"
cleanup_pin() { rm -f -- "${pin_temporary}"; }
trap cleanup_pin EXIT
printf '%s\n' "${revision}" >"${pin_temporary}"
chmod 0644 "${pin_temporary}"
mv -fT -- "${pin_temporary}" "${model_dir}/.pinned-revision"
trap - EXIT
printf '%s\n' "${model_dir}"
