#!/usr/bin/env bash
set -euo pipefail

[[ "${AUDIO8_SGLANG_EXPERIMENTAL:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_EXPERIMENTAL=1 to build the experimental backend." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
lock="${root}/RUNTIME.lock.json"

identity_values="$(
  python3 "${root}/runtime_identity.py" values "${lock}" "${root}"
)"
mapfile -t values <<<"${identity_values}"
[[ "${#values[@]}" == 8 ]] || exit 2
image="${AUDIO8_SGLANG_IMAGE:-${values[3]}}"

docker build --pull=false \
  --build-arg "BASE_IMAGE=${values[0]}" \
  --build-arg "SGLANG_OMNI_COMMIT=${values[1]}" \
  --build-arg "AUDIO8_TTS_COMMIT=${values[2]}" \
  --build-arg "AUDIO8_RUNTIME_FINGERPRINT=${values[7]}" \
  --tag "${image}" \
  --file "${root}/Dockerfile" \
  "${root}"

identity_after="$(
  python3 "${root}/runtime_identity.py" values "${lock}" "${root}"
)"
[[ "${identity_after}" == "${identity_values}" ]] || {
  echo "Audio8 SGLang runtime inputs changed during the image build." >&2
  exit 1
}
image_labels="$(
  docker image inspect --format '{{json .Config.Labels}}' "${image}"
)"
printf '%s\n' "${image_labels}" | \
  python3 "${root}/runtime_identity.py" verify-labels "${lock}" "${root}"
