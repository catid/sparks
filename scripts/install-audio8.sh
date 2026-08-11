#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
action="${1:-verify}"
service_user="${SPARK_SERVICE_USER:-${SUDO_USER:-${USER:-$(id -un)}}}"
image="${AUDIO8_IMAGE:-cerberus/audio8-tts:0.6b-f9612f13}"
unit_target="/etc/systemd/system/cerberus3-audio8.service"
legacy_unit="cerebrus3-audio8.service"
legacy_unit_target="/etc/systemd/system/${legacy_unit}"
private_config="/etc/default/cerberus3-audio8"
legacy_private_config="/etc/default/cerebrus3-audio8"

usage() {
  cat <<'EOF'
Usage: install-audio8.sh [verify|prepare|install|enable|start]

verify   Validate repository inputs without changing the host.
prepare  Download the pinned checkpoint and build the isolated image.
install  Install the systemd unit, but do not enable or start it.
enable   Install and enable the unit for the next boot.
start    Install, enable, and start the service now.

Environment:
  SPARK_SERVICE_USER  Service account (default: invoking account)
  AUDIO8_IMAGE        Local image tag
  AUDIO8_MODEL_ROOT   Checkpoint parent (default: ~/models)
EOF
}

case "${action}" in
  verify|prepare|install|enable|start) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (($# > 1)); then
  usage >&2
  exit 2
fi

[[ "${service_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || {
  echo "Unsafe service user: ${service_user}" >&2
  exit 2
}
service_home="$(getent passwd "${service_user}" | cut -d: -f6)"
service_group="$(id -gn "${service_user}")"
[[ "${service_group}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || {
  echo "Unsafe service group: ${service_group}" >&2
  exit 2
}
[[ "${root}" =~ ^/[A-Za-z0-9._/@+-]+$ &&
   "${service_home}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
  echo "Checkout and service home must be safe absolute paths." >&2
  exit 2
}
for input in \
  "${root}/audio8/Dockerfile" \
  "${root}/audio8/server.py" \
  "${root}/audio8/MODEL.lock.json" \
  "${root}/systemd/cerberus3-audio8.service.in"; do
  [[ -f "${input}" && ! -L "${input}" ]] || {
    echo "Missing regular Audio8 input: ${input}" >&2
    exit 2
  }
done
python3 -m json.tool "${root}/audio8/MODEL.lock.json" >/dev/null
for shell_input in \
  "${root}/audio8/download-model.sh" \
  "${root}/audio8/run-server.sh" \
  "${root}/audio8/synthesize-test.sh" \
  "${root}/audio8/play-loop.sh"; do
  bash -n "${shell_input}"
done
python3 - "${root}/audio8/server.py" <<'PY'
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_bytes()
compile(source, sys.argv[1], "exec")
PY

if [[ "${action}" == verify ]]; then
  echo "Audio8 service inputs are valid."
  exit 0
fi
case "$(hostname -s)" in
  cerberus3|spark3) ;;
  *) echo "Audio8 is assigned to cerberus3; refusing this host." >&2; exit 2 ;;
esac

if [[ "${action}" == prepare ]]; then
  AUDIO8_MODEL_ROOT="${AUDIO8_MODEL_ROOT:-${service_home}/models}" \
    "${root}/audio8/download-model.sh"
  docker build \
    --pull=false \
    --tag "${image}" \
    --file "${root}/audio8/Dockerfile" \
    "${root}/audio8"
  exit 0
fi

mapfile -t model_values < <(python3 - "${root}/audio8/MODEL.lock.json" <<'PY'
import json
import pathlib
import sys

data = json.loads(pathlib.Path(sys.argv[1]).read_text())
for key in ("revision", "local_directory"):
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"invalid {key} in model lock")
    print(value)
PY
)
[[ "${#model_values[@]}" == 2 ]] || {
  echo "Could not read Audio8 model lock." >&2
  exit 2
}
revision="${model_values[0]}"
model_root="${AUDIO8_MODEL_ROOT:-${service_home}/models}"
model_dir="${model_root}/${model_values[1]}"
pin_file="${model_dir}/.pinned-revision"
[[ "${model_root}" =~ ^/[A-Za-z0-9._/@+-]+$ && ! -L "${model_root}" ]] || {
  echo "Unsafe Audio8 model root: ${model_root}" >&2
  exit 2
}
[[ -d "${model_dir}" && ! -L "${model_dir}" ]] || {
  echo "Pinned checkpoint is absent; run prepare first." >&2
  exit 1
}
for required in config.json model.safetensors codec.pth processing_arktts.py; do
  [[ -f "${model_dir}/${required}" && -s "${model_dir}/${required}" &&
     ! -L "${model_dir}/${required}" ]] || {
    echo "Pinned checkpoint is incomplete; run prepare first." >&2
    exit 1
  }
done
[[ -f "${pin_file}" && ! -L "${pin_file}" &&
   "$(<"${pin_file}")" == "${revision}" ]] || {
  echo "Checkpoint revision does not match the model lock; run prepare first." >&2
  exit 1
}
docker image inspect "${image}" >/dev/null || {
  echo "Audio8 image is absent; run prepare first." >&2
  exit 1
}

tmp_dir="$(mktemp -d)"
cleanup() { rm -rf -- "${tmp_dir}"; }
trap cleanup EXIT
rendered="${tmp_dir}/cerberus3-audio8.service"
sed_escape() { sed 's/[\&|]/\\&/g' <<<"$1"; }
sed \
  -e "s|@PROJECT_DIR@|$(sed_escape "${root}")|g" \
  -e "s|@HOME@|$(sed_escape "${service_home}")|g" \
  -e "s|@MODEL_DIR@|$(sed_escape "${model_dir}")|g" \
  -e "s|@USER@|$(sed_escape "${service_user}")|g" \
  -e "s|@GROUP@|$(sed_escape "${service_group}")|g" \
  "${root}/systemd/cerberus3-audio8.service.in" >"${rendered}"
systemd-analyze verify "${rendered}" >/dev/null

if systemctl cat "${legacy_unit}" >/dev/null 2>&1; then
  sudo systemctl stop "${legacy_unit}"
  sudo systemctl disable "${legacy_unit}"
fi
if systemctl is-active --quiet "${legacy_unit}"; then
  echo "Legacy Audio8 unit remained active after stop." >&2
  exit 1
fi
docker rm -f cerebrus3-audio8 >/dev/null 2>&1 || true
if docker ps --format '{{.Names}}' | grep -Fxq cerebrus3-audio8; then
  echo "Legacy Audio8 container remained active after stop." >&2
  exit 1
fi

# Copy the private reference-voice override atomically without ever printing
# or interpolating its contents. Existing canonical configuration always wins
# on repeat runs.
sudo python3 - "${legacy_private_config}" "${private_config}" <<'PY'
import os
import pathlib
import shutil
import stat
import tempfile
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])

def checked(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600):
        raise SystemExit(f"unsafe private Audio8 configuration: {path}")
    return info

if destination.exists() or destination.is_symlink():
    checked(destination)
elif source.exists() or source.is_symlink():
    checked(source)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.migration.", dir=destination.parent
    )
    temporary = pathlib.Path(temporary_name)
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(source_descriptor, "rb", closefd=False) as source_file:
                with os.fdopen(descriptor, "wb", closefd=False) as destination_file:
                    shutil.copyfileobj(source_file, destination_file, 1024 * 1024)
                    destination_file.flush()
                    os.fsync(descriptor)
        finally:
            os.close(source_descriptor)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, 0, 0)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
PY

sudo install -o root -g root -m 0644 "${rendered}" "${unit_target}"
sudo rm -f -- "${legacy_unit_target}"
sudo systemctl daemon-reload
sudo systemctl reset-failed "${legacy_unit}" >/dev/null 2>&1 || true

case "${action}" in
  install) ;;
  enable) sudo systemctl enable cerberus3-audio8.service ;;
  start) sudo systemctl enable --now cerberus3-audio8.service ;;
esac
echo "Audio8 service ${action} completed."
