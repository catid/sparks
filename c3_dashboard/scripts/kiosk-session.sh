#!/usr/bin/env bash

set -euo pipefail

dashboard_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
kiosk_program="${dashboard_dir}/kiosk.py"
url="${C3_KIOSK_URL:-http://127.0.0.1:9763/}"
mode="${C3_KIOSK_MODE:-1424x280}"
configured_output="${C3_KIOSK_OUTPUT:-auto}"
retry_seconds="${C3_KIOSK_RETRY_SECONDS:-5}"
wait_seconds="${C3_KIOSK_OUTPUT_WAIT_SECONDS:-10}"

fail() {
  echo "C3 kiosk session: $*" >&2
  exit 75
}

[[ "${configured_output}" == "auto" ||
   "${configured_output}" =~ ^[A-Za-z0-9_.:-]+$ ]] ||
  fail "invalid output name ${configured_output}"
[[ "${mode}" =~ ^[1-9][0-9]{1,4}x[1-9][0-9]{1,4}$ ]] ||
  fail "invalid display mode ${mode}"
[[ "${retry_seconds}" =~ ^[1-9][0-9]{0,2}$ && "${retry_seconds}" -le 300 ]] ||
  fail "invalid page retry interval ${retry_seconds}"
[[ "${wait_seconds}" =~ ^[1-9][0-9]{0,2}$ && "${wait_seconds}" -le 300 ]] ||
  fail "invalid output wait interval ${wait_seconds}"

for required_command in xrandr xset xsetroot dbus-run-session python3; do
  command -v "${required_command}" >/dev/null 2>&1 ||
    fail "missing required command ${required_command}"
done

connected_outputs() {
  xrandr --query 2>/dev/null | awk '$2 == "connected" { print $1 }'
}

output=""
for ((attempt = 1; attempt <= wait_seconds; attempt++)); do
  mapfile -t connected < <(connected_outputs)
  candidates=("${connected[@]}")
  if [[ "${configured_output}" != "auto" ]]; then
    candidates=("${configured_output}")
  fi
  for candidate in "${candidates[@]}"; do
    candidate_connected=0
    for connector in "${connected[@]}"; do
      if [[ "${connector}" == "${candidate}" ]]; then
        candidate_connected=1
        break
      fi
    done
    if [[ "${candidate_connected}" != "1" ]]; then
      continue
    fi
    xrandr_arguments=()
    for connector in "${connected[@]}"; do
      if [[ "${connector}" != "${candidate}" ]]; then
        xrandr_arguments+=(--output "${connector}" --off)
      fi
    done
    xrandr_arguments+=(
      --output "${candidate}" --mode "${mode}" --pos 0x0 --primary
      --fb "${mode}"
    )
    if xrandr "${xrandr_arguments[@]}"; then
      output="${candidate}"
      break 2
    fi
  done
  sleep 1
done

[[ -n "${output}" ]] ||
  fail "no connected output accepted native mode ${mode} after ${wait_seconds}s"
echo "C3 kiosk session: using ${output} at ${mode}" >&2

# Make uncovered X pixels and WebKit's startup/reload interval exact RGB zero.
# The in-page TFT maintenance band independently traverses the WebKit viewport.
xsetroot -solid black

# Screen blanking is undesirable for an always-on status panel.  Failure is
# non-fatal because a display can legitimately omit DPMS support.
xset s off || true
xset s noblank || true
xset -dpms || true

exec dbus-run-session -- \
  python3 "${kiosk_program}" \
  --url "${url}" --size "${mode}" --retry-seconds "${retry_seconds}"
