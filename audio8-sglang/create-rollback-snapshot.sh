#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID}" == 0 ]] || {
  echo "Rollback snapshot creation must run as root." >&2
  exit 2
}
[[ "${AUDIO8_SGLANG_PRODUCTION:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_PRODUCTION=1 to create the rollback snapshot." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
state_root=/var/lib/cerberus3-audio8-sglang
legacy_root="${state_root}/stock-rollback"
snapshot_root="${state_root}/stock-rollback-v2"
runtime_root=/usr/local/lib/cerberus3-audio8-stock-rollback-v2
image_reference=cerberus/audio8-tts:0.6b-f9612f13
runtime_stage=
snapshot_stage=
umask 077

cleanup() {
  for path in "${runtime_stage:-}" "${snapshot_stage:-}"; do
    case "${path}" in
      /usr/local/lib/.cerberus3-audio8-stock-rollback-v2.*|\
      "${state_root}"/.stock-rollback-v2.*)
        [[ ! -e "${path}" || -d "${path}" ]] || continue
        rm -rf -- "${path}"
        ;;
    esac
  done
}
trap cleanup EXIT

if [[ -e "${snapshot_root}" ]]; then
  "${root}/validate-rollback-snapshot.sh"
  exit 0
fi

if [[ -e "${legacy_root}/cerberus3-audio8.service" ]]; then
  source_unit="${legacy_root}/cerberus3-audio8.service"
  source_image_id_file="${legacy_root}/stock-image-id"
  source_enabled_file="${legacy_root}/stock-enabled-state"
  for source in "${source_unit}" "${source_image_id_file}" \
    "${source_enabled_file}"; do
    [[ -f "${source}" && ! -L "${source}" ]] || {
      echo "Legacy rollback snapshot is incomplete: ${source}" >&2
      exit 1
    }
  done
  image_id="$(<"${source_image_id_file}")"
  enabled_state="$(<"${source_enabled_file}")"
  source_default=
  if [[ -f "${legacy_root}/cerberus3-audio8.default" &&
        ! -L "${legacy_root}/cerberus3-audio8.default" ]]; then
    source_default="${legacy_root}/cerberus3-audio8.default"
  fi
else
  source_unit=/etc/systemd/system/cerberus3-audio8.service
  [[ -f "${source_unit}" && ! -L "${source_unit}" ]] || {
    echo "Cannot snapshot an unsafe stock Audio8 unit." >&2
    exit 1
  }
  image_id="$(docker image inspect --format '{{.Id}}' "${image_reference}")"
  enabled_state="$(systemctl is-enabled cerberus3-audio8.service 2>/dev/null || true)"
  source_default=
  if [[ -f /etc/default/cerberus3-audio8 &&
        ! -L /etc/default/cerberus3-audio8 ]]; then
    source_default=/etc/default/cerberus3-audio8
  fi
fi

[[ "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
  echo "Stock rollback image ID is invalid." >&2
  exit 1
}
actual_image_id="$(docker image inspect --format '{{.Id}}' "${image_reference}")"
[[ "${actual_image_id}" == "${image_id}" ]] || {
  echo "Stock Audio8 image no longer matches the rollback source." >&2
  exit 1
}
[[ "${enabled_state}" == enabled || "${enabled_state}" == disabled ]] || {
  echo "Stock rollback enabled state is unsupported: ${enabled_state}" >&2
  exit 1
}
mapfile -t exec_starts < <(sed -n 's/^ExecStart=//p' "${source_unit}")
[[ "${#exec_starts[@]}" == 1 ]] || {
  echo "Stock Audio8 unit must have exactly one ExecStart." >&2
  exit 1
}
source_launcher="${exec_starts[0]}"
[[ "${source_launcher}" =~ ^/[A-Za-z0-9._/@+-]+$ &&
   -f "${source_launcher}" && ! -L "${source_launcher}" &&
   "$(readlink -f "${source_launcher}")" == "${source_launcher}" ]] || {
  echo "Stock Audio8 launcher is missing or unsafe." >&2
  exit 1
}
source_lock="$(dirname "${source_launcher}")/MODEL.lock.json"
[[ -f "${source_lock}" && ! -L "${source_lock}" &&
   "$(readlink -f "${source_lock}")" == "${source_lock}" ]] || {
  echo "Stock Audio8 model lock is missing or unsafe." >&2
  exit 1
}

runtime_stage="$(mktemp -d /usr/local/lib/.cerberus3-audio8-stock-rollback-v2.XXXXXX)"
chmod 0755 "${runtime_stage}"
install -d -o root -g root -m 0755 "${runtime_stage}/audio8"
install -o root -g root -m 0755 \
  "${source_launcher}" "${runtime_stage}/audio8/run-server.sh"
install -o root -g root -m 0644 \
  "${source_lock}" "${runtime_stage}/audio8/MODEL.lock.json"
(
  cd "${runtime_stage}"
  sha256sum audio8/run-server.sh audio8/MODEL.lock.json >SHA256SUMS
)
chown root:root "${runtime_stage}/SHA256SUMS"
chmod 0644 "${runtime_stage}/SHA256SUMS"

if [[ -e "${runtime_root}" ]]; then
  [[ -d "${runtime_root}" && ! -L "${runtime_root}" &&
     "$(stat -c '%u:%g:%a' "${runtime_root}")" == 0:0:755 ]] || {
    echo "Existing rollback runtime is unsafe." >&2
    exit 1
  }
  for specification in \
    "${runtime_root}/audio8:0:0:755:directory" \
    "${runtime_root}/audio8/run-server.sh:0:0:755:file" \
    "${runtime_root}/audio8/MODEL.lock.json:0:0:644:file" \
    "${runtime_root}/SHA256SUMS:0:0:644:file"; do
    IFS=: read -r path uid gid mode kind <<<"${specification}"
    [[ ! -L "${path}" && "$(stat -c '%u:%g:%a' "${path}")" == \
      "${uid}:${gid}:${mode}" ]] || {
      echo "Existing rollback runtime metadata is invalid: ${path}" >&2
      exit 1
    }
    if [[ "${kind}" == directory ]]; then
      [[ -d "${path}" ]] || exit 1
    else
      [[ -f "${path}" ]] || exit 1
    fi
  done
  cmp -s "${runtime_stage}/SHA256SUMS" "${runtime_root}/SHA256SUMS" || {
    echo "Existing rollback runtime differs from the stock source." >&2
    exit 1
  }
  (cd "${runtime_root}" && sha256sum --strict --check SHA256SUMS >/dev/null)
else
  mv -T -- "${runtime_stage}" "${runtime_root}"
  runtime_stage=
  sync -f /usr/local/lib
fi

snapshot_stage="$(mktemp -d "${state_root}/.stock-rollback-v2.XXXXXX")"
chmod 0700 "${snapshot_stage}"
install -o root -g root -m 0600 \
  "${source_unit}" "${snapshot_stage}/cerberus3-audio8.original.service"
python3 - "${source_unit}" "${snapshot_stage}/cerberus3-audio8.service" \
  "${source_launcher}" "${runtime_root}" <<'PY'
import pathlib
import sys

source, destination, old_launcher, runtime_root = sys.argv[1:]
text = pathlib.Path(source).read_text(encoding="utf-8")
old_exec = f"ExecStart={old_launcher}"
new_exec = f"ExecStart={runtime_root}/audio8/run-server.sh"
if text.splitlines().count(old_exec) != 1:
    raise SystemExit("stock unit launcher changed during snapshot")
working = [line for line in text.splitlines() if line.startswith("WorkingDirectory=")]
if len(working) != 1:
    raise SystemExit("stock unit must have exactly one WorkingDirectory")
text = text.replace(old_exec, new_exec).replace(
    working[0], f"WorkingDirectory={runtime_root}"
)
pathlib.Path(destination).write_text(text, encoding="utf-8")
PY
chmod 0600 "${snapshot_stage}/cerberus3-audio8.service"
printf '2\n' >"${snapshot_stage}/snapshot-version"
printf '%s\n' "${image_id}" >"${snapshot_stage}/stock-image-id"
printf '%s\n' "${image_reference}" >"${snapshot_stage}/stock-image-reference"
printf '%s\n' "${enabled_state}" >"${snapshot_stage}/stock-enabled-state"
if [[ -n "${source_default}" ]]; then
  install -o root -g root -m 0600 \
    "${source_default}" "${snapshot_stage}/cerberus3-audio8.default"
  printf 'yes\n' >"${snapshot_stage}/stock-default-present"
else
  printf 'no\n' >"${snapshot_stage}/stock-default-present"
fi
sha256sum "${runtime_root}/SHA256SUMS" | awk '{print $1}' \
  >"${snapshot_stage}/runtime-manifest-sha256"
chmod 0600 "${snapshot_stage}"/*
manifest_files=(
  snapshot-version cerberus3-audio8.service
  cerberus3-audio8.original.service stock-image-id stock-image-reference
  stock-enabled-state stock-default-present runtime-manifest-sha256
)
if [[ -f "${snapshot_stage}/cerberus3-audio8.default" ]]; then
  manifest_files+=(cerberus3-audio8.default)
fi
(
  cd "${snapshot_stage}"
  sha256sum "${manifest_files[@]}" >SHA256SUMS
  sha256sum --strict --check SHA256SUMS >/dev/null
)
chmod 0600 "${snapshot_stage}/SHA256SUMS"
grep -Fxq "WorkingDirectory=${runtime_root}" \
  "${snapshot_stage}/cerberus3-audio8.service"
grep -Fxq "ExecStart=${runtime_root}/audio8/run-server.sh" \
  "${snapshot_stage}/cerberus3-audio8.service"
[[ "$(grep -c '^ExecStart=' "${snapshot_stage}/cerberus3-audio8.service")" == 1 ]]
mv -T -- "${snapshot_stage}" "${snapshot_root}"
snapshot_stage=
sync -f "${state_root}"
"${root}/validate-rollback-snapshot.sh"
