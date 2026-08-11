#!/usr/bin/env bash
set -euo pipefail

[[ "${EUID}" == 0 ]] || {
  echo "Production installation must run as root." >&2
  exit 2
}
[[ "${AUDIO8_SGLANG_PRODUCTION:-0}" == 1 ]] || {
  echo "Set AUDIO8_SGLANG_PRODUCTION=1 to install production artifacts." >&2
  exit 2
}

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
runtime_root=/usr/local/lib/cerberus3-audio8-sglang
state_root=/var/lib/cerberus3-audio8-sglang
rollback_root="${state_root}/stock-rollback"
unit_root=/etc/systemd/system

for directory in "${runtime_root}" "${state_root}" "${rollback_root}"; do
  [[ ! -L "${directory}" ]] || {
    echo "Refusing symlinked production directory: ${directory}" >&2
    exit 1
  }
done
install -d -o root -g root -m 0755 "${runtime_root}"
install -d -o root -g root -m 0755 "${runtime_root}/patches"
install -d -o root -g root -m 0755 "${runtime_root}/systemd"
install -d -o root -g root -m 0700 "${state_root}" "${rollback_root}"
install -d -o root -g root -m 0700 /etc/cerberus3-audio8-sglang
install -d -o catid -g catid -m 0700 \
  /home/catid/.cache/cerberus-audio8-sglang

runtime_files=(
  Dockerfile RUNTIME.lock.json check_health.py check_stock_health.py gateway.py
  prepare_cache.py runtime_identity.py validate_reference.py
  verify_source_contract.py
)
runtime_scripts=(
  cutover-production.sh ensure-production-networks.sh run-production-backend.sh
  rollback-to-stock.sh run-production-gateway.sh supervise-production-backend.sh
  wait-production-gateway.sh
)
for name in "${runtime_files[@]}"; do
  [[ ! -L "${runtime_root}/${name}" ]] || exit 1
  install -o root -g root -m 0644 "${source_root}/${name}" "${runtime_root}/${name}"
done
for name in "${runtime_scripts[@]}"; do
  [[ ! -L "${runtime_root}/${name}" ]] || exit 1
  install -o root -g root -m 0755 "${source_root}/${name}" "${runtime_root}/${name}"
done
for source in "${source_root}"/patches/*.patch; do
  name="$(basename "${source}")"
  [[ ! -L "${runtime_root}/patches/${name}" ]] || exit 1
  install -o root -g root -m 0644 "${source}" "${runtime_root}/patches/${name}"
done
for source in "${source_root}"/systemd/*; do
  name="$(basename "${source}")"
  [[ ! -L "${runtime_root}/systemd/${name}" ]] || exit 1
  install -o root -g root -m 0644 \
    "${source}" "${runtime_root}/systemd/${name}"
done

source_identity="$(python3 "${source_root}/runtime_identity.py" values \
  "${source_root}/RUNTIME.lock.json" "${source_root}")"
runtime_identity="$(python3 "${runtime_root}/runtime_identity.py" values \
  "${runtime_root}/RUNTIME.lock.json" "${runtime_root}")"
[[ "${source_identity}" == "${runtime_identity}" ]] || {
  echo "Installed runtime identity differs from source." >&2
  exit 1
}

if [[ ! -e "${rollback_root}/cerberus3-audio8.service" ]]; then
  install -o root -g root -m 0600 \
    "${unit_root}/cerberus3-audio8.service" \
    "${rollback_root}/cerberus3-audio8.service"
  if [[ -f /etc/default/cerberus3-audio8 ]]; then
    install -o root -g root -m 0600 /etc/default/cerberus3-audio8 \
      "${rollback_root}/cerberus3-audio8.default"
  fi
  docker image inspect --format '{{.Id}}' cerberus/audio8-tts:0.6b-f9612f13 \
    | tee "${rollback_root}/stock-image-id" >/dev/null
  systemctl is-enabled cerberus3-audio8.service \
    >"${rollback_root}/stock-enabled-state" || true
  chmod 0600 "${rollback_root}/stock-image-id" \
    "${rollback_root}/stock-enabled-state"
fi
for unit in cerberus3-audio8-sglang-network.service \
  cerberus3-audio8-sglang-backend.service; do
  install -o root -g root -m 0644 "${source_root}/systemd/${unit}" \
    "${unit_root}/${unit}"
done
install -o root -g root -m 0600 "${source_root}/systemd/backend.conf" \
  /etc/cerberus3-audio8-sglang/backend.env
systemctl daemon-reload
systemctl enable cerberus3-audio8-sglang-network.service \
  cerberus3-audio8-sglang-backend.service >/dev/null
# Remove the superseded separately named gateway from pre-production installs.
# The cutover installs this implementation under the stable canonical Audio8
# service name so existing voice dependencies never need to change.
systemctl disable --now cerberus3-audio8-sglang-gateway.service \
  >/dev/null 2>&1 || true
rm -f -- "${unit_root}/cerberus3-audio8-sglang-gateway.service"
systemctl daemon-reload
