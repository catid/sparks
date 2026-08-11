#!/usr/bin/env bash

set -euo pipefail

action="${1:-verify}"
config_file="${AVAHI_CONFIG_FILE:-/etc/avahi/avahi-daemon.conf}"
management_iface="${CERBERUS_MDNS_INTERFACE:-enP7s7}"

usage() {
  cat <<'EOF'
Usage: install-cerberus-mdns.sh [verify|apply]

Publish each Cerberus hostname through mDNS on the DHCP management interface
only. This makes cerberus1.local through cerberus3.local stable without
publishing ConnectX ring addresses.

Environment:
  AVAHI_CONFIG_FILE        default: /etc/avahi/avahi-daemon.conf
                           overrides are verify-only
  CERBERUS_MDNS_INTERFACE default: enP7s7
EOF
}

case "${action}" in
  verify|apply) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
[[ "${management_iface}" =~ ^[A-Za-z0-9._:-]+$ ]] || {
  echo "Unsafe management interface: ${management_iface}" >&2
  exit 2
}
[[ -f "${config_file}" && ! -L "${config_file}" ]] || {
  echo "Avahi configuration must be a regular, non-symlink file: ${config_file}" >&2
  exit 2
}
if [[ "${action}" == apply && "${config_file}" != /etc/avahi/avahi-daemon.conf ]]; then
  echo "AVAHI_CONFIG_FILE overrides are allowed only for verify." >&2
  exit 2
fi

case "$(hostname -s)" in
  cerberus1|cerberus2|cerberus3) ;;
  *) echo "Run after installing a canonical cerberus1-3 hostname." >&2; exit 2 ;;
esac

rendered="$(mktemp)"
cleanup() { rm -f -- "${rendered}"; }
trap cleanup EXIT
awk -v iface="${management_iface}" '
  BEGIN { written = 0 }
  /^[#;]?allow-interfaces=/ {
    if (!written) print "allow-interfaces=" iface
    written = 1
    next
  }
  { print }
  END {
    if (!written) {
      print ""
      print "[server]"
      print "allow-interfaces=" iface
    }
  }
' "${config_file}" >"${rendered}"

grep -Fxq "allow-interfaces=${management_iface}" "${rendered}"
[[ "$(grep -Ec '^[^#;]*allow-interfaces=' "${rendered}")" == 1 ]]

if [[ "${action}" == verify ]]; then
  diff -u -- "${config_file}" "${rendered}" || true
  echo "Verified management-only mDNS on ${management_iface}; no change made."
  exit 0
fi

ip link show dev "${management_iface}" >/dev/null
command -v avahi-daemon >/dev/null
sudo -n true
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/etc/avahi/avahi-daemon.conf.before-cerberus-${timestamp}"
sudo -n test ! -e "${backup}"
sudo -n install -o root -g root -m 0644 -- "${config_file}" "${backup}"

rollback() {
  status=$?
  trap - EXIT
  if ((status != 0)); then
    sudo -n install -o root -g root -m 0644 -- "${backup}" "${config_file}" || true
    sudo -n systemctl restart avahi-daemon.service || true
  fi
  cleanup
  exit "${status}"
}
trap rollback EXIT
sudo -n install -o root -g root -m 0644 -- "${rendered}" "${config_file}"
sudo -n systemctl restart avahi-daemon.service
systemctl is-active --quiet avahi-daemon.service
trap - EXIT
cleanup
echo "mDNS now publishes only on ${management_iface}; backup: ${backup}"
