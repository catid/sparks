#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

usage() {
  cat <<'EOF'
Usage: install-cluster-host-identity.sh cerebrus1|cerebrus2|cerebrus3 [--apply]

Validate or install the canonical short hostname and complete three-node
/etc/hosts map. The selected role is bound to its expected management IPv4
address before any write. Without --apply this is a read-only preview.

Environment:
  CLUSTER_MGMT_IFACE  default: enP7s7
EOF
}

role="${1:-}"
shift || true
apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1; shift ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
(($# == 0)) || { usage >&2; exit 2; }

case "${role}" in
  cerebrus1|cerebrus2|cerebrus3) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

source_hosts="${repo_root}/hosts/${role}.hosts"
mgmt_iface="${CLUSTER_MGMT_IFACE:-enP7s7}"
[[ -f "${source_hosts}" && ! -L "${source_hosts}" ]] || {
  echo "Missing regular hosts source: ${source_hosts}" >&2
  exit 2
}
[[ "${mgmt_iface}" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "Unsafe management interface: ${mgmt_iface}" >&2
  exit 2
}
command -v ip >/dev/null
command -v getent >/dev/null

declare -A peer_ips=()
declare -A peer_lines=()
for peer in cerebrus1 cerebrus2 cerebrus3; do
  mapping="$(awk -v alias="${peer}.lan" '
    $1 !~ /^#/ {
      for (field = 2; field <= NF; field++) {
        if ($field == alias) { print; exit }
      }
    }
  ' "${source_hosts}")"
  [[ -n "${mapping}" ]] || {
    echo "Hosts source lacks a management mapping for ${peer}." >&2
    exit 1
  }
  read -r peer_ip _ <<<"${mapping}"
  [[ "${peer_ip}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || {
    echo "Hosts source has an invalid IPv4 address for ${peer}: ${peer_ip}" >&2
    exit 1
  }
  IFS=. read -r -a octets <<<"${peer_ip}"
  for octet in "${octets[@]}"; do
    ((10#${octet} <= 255)) || {
      echo "Hosts source has an invalid IPv4 address for ${peer}: ${peer_ip}" >&2
      exit 1
    }
  done
  legacy="${peer/cerebrus/spark}"
  for alias in "${peer}.lan" "${peer}" "${legacy}.lan" "${legacy}"; do
    [[ " ${mapping} " == *" ${alias} "* ]] || {
      echo "${peer} mapping lacks required alias ${alias}." >&2
      exit 1
    }
  done
  peer_ips["${peer}"]="${peer_ip}"
  peer_lines["${peer}"]="${mapping}"
done
expected_ip="${peer_ips[${role}]}"

mapfile -t management_ips < <(
  ip -4 -o addr show dev "${mgmt_iface}" scope global |
    awk '{split($4, address, "/"); print address[1]}'
)
printf '%s\n' "${management_ips[@]}" | grep -Fxq -- "${expected_ip}" || {
  echo "${role} requires ${expected_ip} on ${mgmt_iface}; observed: ${management_ips[*]:-none}" >&2
  exit 1
}

grep -Fxq -- "127.0.1.1 ${role}" "${source_hosts}" || {
  echo "Hosts source lacks the role-specific loopback mapping." >&2
  exit 1
}

staged_hosts="$(mktemp)"
cleanup_staged() {
  rm -f -- "${staged_hosts}"
}
trap cleanup_staged EXIT
awk -v role="${role}" \
    -v c1_ip="${peer_ips[cerebrus1]}" \
    -v c2_ip="${peer_ips[cerebrus2]}" \
    -v c3_ip="${peer_ips[cerebrus3]}" \
    -v c1_line="${peer_lines[cerebrus1]}" \
    -v c2_line="${peer_lines[cerebrus2]}" \
    -v c3_line="${peer_lines[cerebrus3]}" '
  function cluster_alias(value) {
    return value ~ /^(cerebrus[123](\.lan)?|spark[123](\.lan)?)$/
  }
  function cluster_ip(value) {
    return value == c1_ip || value == c2_ip || value == c3_ip
  }
  $0 == "# BEGIN SPARKS CLUSTER HOSTS" { in_managed = 1; next }
  $0 == "# END SPARKS CLUSTER HOSTS" { in_managed = 0; next }
  in_managed { next }
  $1 == "127.0.1.1" {
    line = "127.0.1.1 " role
    for (field = 2; field <= NF; field++) {
      if ($field ~ /^#/) break
      if (!cluster_alias($field)) line = line " " $field
    }
    print line
    loopback_written = 1
    next
  }
  cluster_ip($1) {
    line = $1
    retained = 0
    for (field = 2; field <= NF; field++) {
      if ($field ~ /^#/) break
      if (!cluster_alias($field)) {
        line = line " " $field
        retained = 1
      }
    }
    if (retained) print line
    next
  }
  $1 !~ /^#/ && NF > 1 {
    line = $1
    retained = 0
    for (field = 2; field <= NF; field++) {
      if ($field ~ /^#/) break
      if (!cluster_alias($field)) {
        line = line " " $field
        retained = 1
      }
    }
    if (retained) print line
    next
  }
  { print }
  END {
    if (!loopback_written) print "127.0.1.1 " role
    print ""
    print "# BEGIN SPARKS CLUSTER HOSTS"
    print "# Managed by install-cluster-host-identity.sh; preserve unrelated entries outside this block."
    print c1_line
    print c2_line
    print c3_line
    print "# END SPARKS CLUSTER HOSTS"
  }
' /etc/hosts >"${staged_hosts}"

if ((apply == 0)); then
  echo "Validated ${role} source against ${mgmt_iface}=${expected_ip}."
  diff -u -- /etc/hosts "${staged_hosts}" || true
  echo "Dry run only; pass --apply to install hostname and hosts map."
  exit 0
fi

command -v hostnamectl >/dev/null
sudo -n true
old_hostname="$(hostname -s)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/etc/hosts.before-sparks-${timestamp}"
sudo -n test ! -e "${backup}" || {
  echo "Refusing to replace existing backup: ${backup}" >&2
  exit 1
}
sudo -n install -m 0644 -o root -g root -- /etc/hosts "${backup}"

changed=1
rollback() {
  status=$?
  trap - EXIT
  if ((status != 0 && changed == 1)); then
    echo "Identity install failed; restoring ${backup} and ${old_hostname}." >&2
    sudo -n install -m 0644 -o root -g root -- "${backup}" /etc/hosts || true
    sudo -n hostnamectl set-hostname "${old_hostname}" || true
  fi
  cleanup_staged
  exit "${status}"
}
trap rollback EXIT

sudo -n install -m 0644 -o root -g root -- "${staged_hosts}" /etc/hosts
sudo -n hostnamectl set-hostname "${role}"
[[ "$(hostname -s)" == "${role}" ]]
for peer in cerebrus1 cerebrus2 cerebrus3; do
  peer_ip="${peer_ips[${peer}]}"
  getent ahostsv4 "${peer}" | awk '{print $1}' | grep -Fxq -- "${peer_ip}" || {
    echo "${peer} does not resolve to ${peer_ip} after installation." >&2
    exit 1
  }
done

changed=0
trap - EXIT
cleanup_staged
echo "Installed hostname ${role} and cluster hosts map; backup: ${backup}"
