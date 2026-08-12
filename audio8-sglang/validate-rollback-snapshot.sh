#!/usr/bin/env bash
set -euo pipefail

snapshot_root=/var/lib/cerberus3-audio8-sglang/stock-rollback-v2
runtime_root=/usr/local/lib/cerberus3-audio8-stock-rollback-v2

require_directory() {
  local path=$1 mode=$2
  [[ -d "${path}" && ! -L "${path}" ]] || {
    echo "Rollback directory is missing or unsafe: ${path}" >&2
    exit 1
  }
  [[ "$(stat -c '%u:%g:%a' "${path}")" == "0:0:${mode}" ]] || {
    echo "Rollback directory metadata is invalid: ${path}" >&2
    exit 1
  }
}

require_file() {
  local path=$1 mode=$2
  [[ -f "${path}" && ! -L "${path}" ]] || {
    echo "Rollback file is missing or unsafe: ${path}" >&2
    exit 1
  }
  [[ "$(stat -c '%u:%g:%a' "${path}")" == "0:0:${mode}" ]] || {
    echo "Rollback file metadata is invalid: ${path}" >&2
    exit 1
  }
}

require_directory "${snapshot_root}" 700
require_directory "${runtime_root}" 755
require_directory "${runtime_root}/audio8" 755
for name in snapshot-version cerberus3-audio8.service \
  cerberus3-audio8.original.service stock-image-id stock-image-reference \
  stock-enabled-state stock-default-present runtime-manifest-sha256 SHA256SUMS; do
  require_file "${snapshot_root}/${name}" 600
done
require_file "${runtime_root}/audio8/run-server.sh" 755
require_file "${runtime_root}/audio8/MODEL.lock.json" 644
require_file "${runtime_root}/SHA256SUMS" 644

[[ "$(<"${snapshot_root}/snapshot-version")" == 2 ]] || {
  echo "Rollback snapshot has an unsupported version." >&2
  exit 1
}
default_present="$(<"${snapshot_root}/stock-default-present")"
case "${default_present}" in
  yes) require_file "${snapshot_root}/cerberus3-audio8.default" 600 ;;
  no) [[ ! -e "${snapshot_root}/cerberus3-audio8.default" ]] || exit 1 ;;
  *) echo "Rollback default-file state is invalid." >&2; exit 1 ;;
esac
enabled_state="$(<"${snapshot_root}/stock-enabled-state")"
[[ "${enabled_state}" == enabled || "${enabled_state}" == disabled ]] || {
  echo "Rollback enabled state is invalid." >&2
  exit 1
}
[[ "$(<"${snapshot_root}/stock-image-reference")" == \
  cerberus/audio8-tts:0.6b-f9612f13 ]] || {
  echo "Rollback image reference is invalid." >&2
  exit 1
}
[[ "$(<"${snapshot_root}/stock-image-id")" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Rollback image ID is invalid." >&2
  exit 1
}

(cd "${snapshot_root}" && sha256sum --strict --check SHA256SUMS >/dev/null)
(cd "${runtime_root}" && sha256sum --strict --check SHA256SUMS >/dev/null)
[[ "$(sha256sum "${runtime_root}/SHA256SUMS" | awk '{print $1}')" == \
  "$(<"${snapshot_root}/runtime-manifest-sha256")" ]] || {
  echo "Rollback runtime manifest does not match the snapshot." >&2
  exit 1
}

grep -Fxq "WorkingDirectory=${runtime_root}" \
  "${snapshot_root}/cerberus3-audio8.service"
grep -Fxq "ExecStart=${runtime_root}/audio8/run-server.sh" \
  "${snapshot_root}/cerberus3-audio8.service"
[[ "$(grep -c '^ExecStart=' "${snapshot_root}/cerberus3-audio8.service")" == 1 ]]

image_reference="$(<"${snapshot_root}/stock-image-reference")"
actual_image="$(docker image inspect --format '{{.Id}}' "${image_reference}")"
[[ "${actual_image}" == "$(<"${snapshot_root}/stock-image-id")" ]] || {
  echo "Stock Audio8 image no longer matches the rollback snapshot." >&2
  exit 1
}
