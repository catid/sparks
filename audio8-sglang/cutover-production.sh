#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID}" == 0 ]] || {
  echo "Production cutover must run as root." >&2
  exit 2
}
[[ "${AUDIO8_SGLANG_CUTOVER:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_CUTOVER=1 to perform the production cutover." >&2
  exit 2
}

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
backend=cerberus3-audio8-sglang-backend
backend_unit=cerberus3-audio8-sglang-backend.service
stock_unit=cerberus3-audio8.service
voice_unit=cerberus3-voice-bridge.service
unit_root=/etc/systemd/system
gateway_source="${root}/systemd/cerberus3-audio8-sglang-gateway.service"
cutover_complete=0
mutation_started=0

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

rollback_on_error() {
  status=$?
  trap - EXIT
  if ((status != 0 && mutation_started == 1 && cutover_complete == 0)); then
    echo "Cutover failed; invoking the preserved stock rollback." >&2
    AUDIO8_SGLANG_ROLLBACK=1 "${root}/rollback-to-stock.sh" || true
  fi
  exit "${status}"
}
trap rollback_on_error EXIT

"${root}/validate-rollback-snapshot.sh"
systemctl is-active --quiet "${backend_unit}"
systemctl is-enabled --quiet "${stock_unit}"
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${backend}")" == healthy ]]
docker exec "${backend}" python3 /opt/cerberus/check_health.py backend

# Preserve the canonical service identity already used by the voice bridge and
# target. Only its implementation changes during this rollback-armed window.
mutation_started=1
systemctl stop "${voice_unit}"
systemctl stop "${stock_unit}"
if ss -ltnH 'sport = :8010' | grep -q .; then
  echo "Port 8010 remained occupied after stock Audio8 stopped." >&2
  exit 1
fi
[[ -f "${gateway_source}" && ! -L "${gateway_source}" ]] || {
  echo "Canonical SGLang gateway unit source is unsafe." >&2
  exit 1
}
atomic_install_unit "${gateway_source}" "${unit_root}/${stock_unit}"
systemctl daemon-reload
systemctl start "${stock_unit}"
python3 "${root}/check_health.py" gateway
systemctl is-enabled --quiet "${stock_unit}"
systemctl is-active --quiet "${stock_unit}"
systemctl show -p ExecStart --value "${stock_unit}" | \
  grep -Fq '/supervise-production-gateway.sh'
systemctl start "${voice_unit}"
systemctl is-active --quiet "${voice_unit}"
cutover_complete=1
