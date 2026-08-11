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

rollback_root=/var/lib/cerberus3-audio8-sglang/stock-rollback
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

test -f "${rollback_root}/cerberus3-audio8.service"
expected_image="$(<"${rollback_root}/stock-image-id")"
actual_image="$(docker image inspect --format '{{.Id}}' \
  cerberus/audio8-tts:0.6b-f9612f13)"
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
fi
systemctl daemon-reload
systemctl start "${stock_unit}"
systemctl is-enabled --quiet "${stock_unit}"
systemctl show -p ExecStart --value "${stock_unit}" | \
  grep -Fq '/run-server.sh'

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
