#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ip
need_command ping
need_command rdma

rank="${1:-}"
[[ "${rank}" =~ ^[012]$ ]] || { echo "Usage: $0 {0|1|2}" >&2; exit 2; }
expected_host="$(rank_host "${rank}")"
expected_mgmt_ip="$(rank_mgmt_ip "${rank}")"
actual_host="$(hostname -s)"
[[ "${actual_host}" == "${expected_host}" ]] || {
  echo "Fabric rank ${rank} expected ${expected_host}, found ${actual_host}." >&2
  exit 1
}

[[ -f "${MIA3_READINESS_HELPER}" && ! -L "${MIA3_READINESS_HELPER}" ]] || {
  echo "Missing regular ring readiness helper: ${MIA3_READINESS_HELPER}" >&2
  exit 1
}
CX7_NODE_ROLE="${expected_host}" "${MIA3_READINESS_HELPER}" \
  --check-once --scope ring --c3-port-map "${CX7_C3_PORT_MAP}"

mgmt_ip="$(ip -4 -o addr show dev "${CONTROL_IFACE}" scope global | awk '{split($4,a,"/"); print a[1]; exit}')"
[[ "${mgmt_ip}" == "${expected_mgmt_ip}" ]] || {
  echo "${expected_host}: ${CONTROL_IFACE} has ${mgmt_ip:-no IPv4}, expected ${expected_mgmt_ip}." >&2
  exit 1
}

eth_devices=(enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1)
rdma_devices=(rocep1s0f0 roceP2p1s0f0 rocep1s0f1 roceP2p1s0f1)

for index in "${!eth_devices[@]}"; do
  eth="${eth_devices[$index]}"
  roce="${rdma_devices[$index]}"
  sysfs="/sys/class/net/${eth}"
  [[ -d "${sysfs}" ]] || { echo "${expected_host}: missing ${eth}." >&2; exit 1; }
  [[ "$(<"${sysfs}/carrier")" == 1 ]] || { echo "${expected_host}: no carrier on ${eth}." >&2; exit 1; }
  [[ "$(<"${sysfs}/mtu")" == 9000 ]] || { echo "${expected_host}: ${eth} MTU is not 9000." >&2; exit 1; }
  speed="$(<"${sysfs}/speed")"
  ((speed >= 200000)) || { echo "${expected_host}: ${eth} speed=${speed}, expected 200000 Mb/s." >&2; exit 1; }
  ip -4 -o addr show dev "${eth}" scope global | grep -q . || {
    echo "${expected_host}: ${eth} has no ring IPv4 address." >&2
    exit 1
  }
  rdma link show | grep -F "${roce}/1" | grep -q 'state ACTIVE physical_state LINK_UP' || {
    echo "${expected_host}: ${roce}/1 is not ACTIVE/LINK_UP." >&2
    exit 1
  }
done

if [[ "${rank}" != 0 ]]; then
  ping -I "${CONTROL_IFACE}" -c 1 -W 2 "${MASTER_ADDR}" >/dev/null || {
    echo "${expected_host}: cannot reach rendezvous ${MASTER_ADDR} via ${CONTROL_IFACE}." >&2
    exit 1
  }
fi

echo "${expected_host}: management and all four 200 Gb/s RoCE functions are ready."
