#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID}" == 0 ]] || {
  echo "Rollback must run as root." >&2
  exit 2
}
[[ "${AUDIO8_SGLANG_ROLLBACK:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_ROLLBACK=1 to restore stock Audio8." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
rollback_root=/var/lib/cerberus3-audio8-sglang/stock-rollback-v2
stock_runtime_root=/usr/local/lib/cerberus3-audio8-stock-rollback-v2
stock_unit=cerberus3-audio8.service
unit_root=/etc/systemd/system

atomic_install_unit() {
  local source=$1 destination=$2 temporary
  temporary="$(mktemp "${unit_root}/.${stock_unit}.XXXXXX")"
  trap 'rm -f -- "${temporary:-}"' RETURN
  install -o root -g root -m 0644 "${source}" "${temporary}"
  sync -f "${temporary}"
  mv -fT -- "${temporary}" "${destination}"
  sync -f "${unit_root}"
  trap - RETURN
}

"${root}/validate-rollback-snapshot.sh"
expected_image="$(<"${rollback_root}/stock-image-id")"
image_reference="$(<"${rollback_root}/stock-image-reference")"
actual_image="$(docker image inspect --format '{{.Id}}' \
  "${image_reference}")"
[[ "${actual_image}" == "${expected_image}" ]] || {
  echo "Stock Audio8 image no longer matches the rollback snapshot." >&2
  exit 1
}

systemctl stop cerberus3-voice-bridge.service
systemctl stop "${stock_unit}" >/dev/null 2>&1 || true
systemctl disable --now cerberus3-audio8-sglang-gateway.service \
  cerberus3-audio8-sglang-backend.service >/dev/null 2>&1 || true
atomic_install_unit "${rollback_root}/cerberus3-audio8.service" \
  "${unit_root}/${stock_unit}"
if [[ -f "${rollback_root}/cerberus3-audio8.default" ]]; then
  install -o root -g root -m 0600 \
    "${rollback_root}/cerberus3-audio8.default" \
    /etc/default/cerberus3-audio8
else
  rm -f -- /etc/default/cerberus3-audio8
fi
systemctl daemon-reload
enabled_state="$(<"${rollback_root}/stock-enabled-state")"
case "${enabled_state}" in
  enabled) systemctl enable "${stock_unit}" >/dev/null ;;
  disabled) systemctl disable "${stock_unit}" >/dev/null ;;
  *) echo "Unsupported saved enabled state: ${enabled_state}" >&2; exit 1 ;;
esac
systemctl start "${stock_unit}"
[[ "$(systemctl is-enabled "${stock_unit}" 2>/dev/null || true)" == \
  "${enabled_state}" ]]
systemctl show -p ExecStart --value "${stock_unit}" | \
  grep -Fq "${stock_runtime_root}/audio8/run-server.sh"

deadline=$((SECONDS + 240))
while ((SECONDS < deadline)); do
  if python3 /usr/local/lib/cerberus3-audio8-sglang/check_stock_health.py; then
    systemctl start cerberus3-voice-bridge.service
    exit 0
  fi
  sleep 2
done
echo "Stock Audio8 did not recover before the rollback deadline." >&2
exit 1
