#!/usr/bin/env bash

set -euo pipefail

dashboard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
session_script="${dashboard_dir}/scripts/kiosk-session.sh"
kiosk_program="${dashboard_dir}/kiosk.py"
url="${C3_KIOSK_URL:-http://127.0.0.1:9763/}"
mode="${C3_KIOSK_MODE:-1424x280}"
retry_seconds="${C3_KIOSK_RETRY_SECONDS:-5}"
display=":0"
virtual_terminal="vt7"

fail() {
  echo "C3 kiosk: $*" >&2
  exit 64
}

[[ "${mode}" =~ ^[1-9][0-9]{1,4}x[1-9][0-9]{1,4}$ ]] ||
  fail "invalid display mode ${mode}"

for required_command in Xorg mcookie python3 startx xauth xinit; do
  command -v "${required_command}" >/dev/null 2>&1 ||
    fail "missing required command ${required_command}"
done
startx_bin="$(command -v startx)"
grep -Fq -- ' -auth ' "${startx_bin}" ||
  fail "installed startx does not advertise Xorg cookie authentication"
[[ -x "${session_script}" ]] || fail "${session_script} is not executable"
[[ -r "${kiosk_program}" ]] || fail "${kiosk_program} is not readable"

# This check also rejects non-loopback URLs.  Keeping the rendered UI on the
# same local origin avoids turning the display process into a general browser.
python3 "${kiosk_program}" --check --url "${url}" --size "${mode}" \
  --retry-seconds "${retry_seconds}" >/dev/null

runtime_home="${C3_KIOSK_RUNTIME_HOME:-${RUNTIME_DIRECTORY:-}}"
if [[ -z "${runtime_home}" ]]; then
  runtime_home="${XDG_RUNTIME_DIR:-}/dgx-spark-c3-kiosk"
fi
[[ "${runtime_home}" == /* && -d "${runtime_home}" &&
   -O "${runtime_home}" && ! -L "${runtime_home}" ]] ||
  fail "runtime home is not a safe service-owned directory: ${runtime_home}"

umask 077
mkdir -p \
  "${runtime_home}/cache" \
  "${runtime_home}/config" \
  "${runtime_home}/data" \
  "${runtime_home}/state"

export HOME="${runtime_home}"
export XDG_CACHE_HOME="${runtime_home}/cache"
export XDG_CONFIG_HOME="${runtime_home}/config"
export XDG_DATA_HOME="${runtime_home}/data"
export XDG_STATE_HOME="${runtime_home}/state"
export XDG_RUNTIME_DIR="${runtime_home}"
export GDK_BACKEND=x11
export GDK_SCALE=1
export GDK_DPI_SCALE=1
export GTK_CSD=0

xorg_bin="$(command -v Xorg)"
# startx wraps xinit with a fresh MIT-MAGIC-COOKIE and passes -auth to Xorg.
# Calling xinit directly here would leave the local UNIX display unprotected.
exec "${startx_bin}" "${session_script}" -- \
  "${xorg_bin}" "${display}" "${virtual_terminal}" \
  -keeptty -nolisten tcp -noreset
