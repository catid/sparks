#!/usr/bin/env bash

set -euo pipefail

action="${1:-verify}"
shift || true
activate=0
case "${1:-}" in
  "") ;;
  --activate) activate=1; shift ;;
  -h|--help) action=help ;;
  *) action=invalid ;;
esac

usage() {
  cat <<'EOF'
Usage: install-cerberus-management.sh [verify|apply] [--activate]

Create or verify the persistent NetworkManager DHCP profile used by the
Cerberus management interface. The profile advertises the canonical short
hostname and never stores a management address.

`apply` changes only NetworkManager's saved profile. It deliberately leaves
the current connection up unless --activate is supplied. Activation can
interrupt SSH, so use --activate only from an attended management session.

Environment:
  CERBERUS_MANAGEMENT_INTERFACE  default: enP7s7
EOF
}

case "${action}" in
  verify|apply) ;;
  help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
((activate == 0)) || [[ "${action}" == apply ]] || {
  echo "--activate is valid only with apply." >&2
  exit 2
}
(($# == 0)) || { usage >&2; exit 2; }

management_iface="${CERBERUS_MANAGEMENT_INTERFACE:-enP7s7}"
profile="cerberus-mgmt"
[[ "${management_iface}" =~ ^[A-Za-z0-9._:-]+$ ]] || {
  echo "Unsafe management interface: ${management_iface}" >&2
  exit 2
}

role="$(hostname -s)"
case "${role}" in
  cerberus1|cerberus2|cerberus3) ;;
  *)
    echo "Run after installing a canonical cerberus1-3 hostname." >&2
    exit 2
    ;;
esac
command -v nmcli >/dev/null

profile_exists() {
  nmcli -g connection.uuid connection show "${profile}" >/dev/null 2>&1
}

property_is() {
  local property="$1" expected="$2" actual
  actual="$(nmcli -g "${property}" connection show "${profile}")"
  [[ "${actual}" == "${expected}" ]]
}

profile_matches() {
  profile_exists &&
    property_is connection.type 802-3-ethernet &&
    property_is connection.interface-name "${management_iface}" &&
    property_is connection.autoconnect yes &&
    property_is connection.autoconnect-priority 100 &&
    property_is ipv4.method auto &&
    property_is ipv4.addresses "" &&
    property_is ipv4.gateway "" &&
    property_is ipv4.dhcp-hostname "${role}" &&
    property_is ipv4.dhcp-send-hostname yes &&
    property_is ipv4.ignore-auto-dns no &&
    property_is ipv6.dhcp-hostname "${role}" &&
    property_is ipv6.dhcp-send-hostname yes
}

verify_profile() {
  profile_exists || {
    echo "Missing NetworkManager profile: ${profile}" >&2
    return 1
  }
  local property expected
  while IFS='|' read -r property expected; do
    property_is "${property}" "${expected}" || {
      echo "${profile}: ${property} must be ${expected}" >&2
      return 1
    }
  done <<EOF
connection.type|802-3-ethernet
connection.interface-name|${management_iface}
connection.autoconnect|yes
connection.autoconnect-priority|100
ipv4.method|auto
ipv4.addresses|
ipv4.gateway|
ipv4.dhcp-hostname|${role}
ipv4.dhcp-send-hostname|yes
ipv4.ignore-auto-dns|no
ipv6.dhcp-hostname|${role}
ipv6.dhcp-send-hostname|yes
EOF
  echo "Verified ${profile}: DHCP on ${management_iface}, hostname ${role}."
}

if [[ "${action}" == verify ]]; then
  verify_profile
  exit 0
fi

command -v ip >/dev/null
ip link show dev "${management_iface}" >/dev/null
sudo -n true

if profile_matches; then
  if ((activate)); then
    echo "Activating ${profile}; the management connection may briefly drop."
    sudo -n nmcli connection up "${profile}" ifname "${management_iface}"
  fi
  verify_profile
  echo "${profile} was already canonical; no saved settings changed."
  exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup=""
created=0
if profile_exists; then
  backup="${profile}-before-${timestamp}"
  sudo -n nmcli connection clone "${profile}" "${backup}"
  sudo -n nmcli connection modify "${backup}" connection.autoconnect no
else
  sudo -n nmcli connection add \
    type ethernet ifname "${management_iface}" con-name "${profile}"
  created=1
fi

rollback() {
  status=$?
  trap - EXIT
  if ((status != 0)); then
    echo "Management profile install failed; restoring the prior profile." >&2
    sudo -n nmcli connection delete "${profile}" >/dev/null 2>&1 || true
    if [[ -n "${backup}" ]]; then
      sudo -n nmcli connection modify "${backup}" \
        connection.id "${profile}" connection.autoconnect yes || true
    fi
  fi
  exit "${status}"
}
trap rollback EXIT

sudo -n nmcli connection modify "${profile}" \
  connection.interface-name "${management_iface}" \
  connection.autoconnect yes \
  connection.autoconnect-priority 100 \
  ipv4.method auto \
  ipv4.addresses "" \
  ipv4.gateway "" \
  ipv4.dhcp-hostname "${role}" \
  ipv4.dhcp-send-hostname yes \
  ipv4.ignore-auto-dns no \
  ipv6.dhcp-hostname "${role}" \
  ipv6.dhcp-send-hostname yes

verify_profile
if ((activate)); then
  echo "Activating ${profile}; the management connection may briefly drop."
  sudo -n nmcli connection up "${profile}" ifname "${management_iface}"
fi

trap - EXIT
if ((created)); then
  echo "Installed ${profile}; it will be selected automatically at boot."
else
  echo "Installed ${profile}; rollback copy retained as ${backup} (autoconnect off)."
fi
