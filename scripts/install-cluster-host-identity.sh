#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

usage() {
  cat <<'EOF'
Usage: install-cluster-host-identity.sh cerberus1|cerberus2|cerberus3 [--apply]

Validate or install the canonical short hostname and role-specific loopback
entry. Management addresses remain DHCP/DNS-owned and are never pinned in
/etc/hosts. The legacy cerebrus1-3 and spark1-3 role spellings are accepted
as migration inputs but always normalize to cerberus1-3. Without --apply this
is a read-only preview.

Environment:
  CLUSTER_HOSTS_FILE  default: /etc/hosts; alternate files are dry-run only
EOF
}

requested_role="${1:-}"
shift || true
apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1; shift ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
(($# == 0)) || { usage >&2; exit 2; }

canonical_role() {
  case "$1" in
    cerberus1|cerebrus1|spark1) printf 'cerberus1\n' ;;
    cerberus2|cerebrus2|spark2) printf 'cerberus2\n' ;;
    cerberus3|cerebrus3|spark3) printf 'cerberus3\n' ;;
    *) return 1 ;;
  esac
}

case "${requested_role}" in
  -h|--help) usage; exit 0 ;;
esac
if ! role="$(canonical_role "${requested_role}")"; then
  usage >&2
  exit 2
fi

source_hosts="${repo_root}/hosts/${role}.hosts"
hosts_file="${CLUSTER_HOSTS_FILE:-/etc/hosts}"
[[ -f "${source_hosts}" && ! -L "${source_hosts}" ]] || {
  echo "Missing regular hosts source: ${source_hosts}" >&2
  exit 2
}
[[ -f "${hosts_file}" && ! -L "${hosts_file}" ]] || {
  echo "Hosts target must be a regular, non-symlink file: ${hosts_file}" >&2
  exit 2
}
if ((apply)) && [[ "${hosts_file}" != /etc/hosts ]]; then
  echo "CLUSTER_HOSTS_FILE overrides are allowed only for a dry run." >&2
  exit 2
fi

grep -Fxq -- "127.0.1.1 ${role}" "${source_hosts}" || {
  echo "Hosts source lacks the role-specific loopback mapping." >&2
  exit 1
}
if awk '
  NF > 0 && $1 !~ /^#/ && $1 != "127.0.0.1" && $1 != "127.0.1.1" &&
    $1 != "::1" && $1 !~ /^ff02::/ { found = 1 }
  END { exit !found }
' "${source_hosts}"; then
  echo "Hosts source must not pin a management or fabric address." >&2
  exit 1
fi

staged_hosts="$(mktemp)"
cleanup_staged() {
  rm -f -- "${staged_hosts}"
}
trap cleanup_staged EXIT
awk -v role="${role}" '
  function cluster_alias(value) {
    return value ~ /^((cerberus|cerebrus|spark)[123])(\.(lan|local))?$/
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
  }
' "${hosts_file}" >"${staged_hosts}"

if ((apply == 0)); then
  echo "Validated canonical role ${role}; management resolution remains DNS-owned."
  diff -u -- "${hosts_file}" "${staged_hosts}" || true
  echo "Dry run only; pass --apply to install hostname and loopback identity."
  exit 0
fi

command -v hostnamectl >/dev/null
sudo -n true
old_hostname="$(hostname -s)"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/etc/hosts.before-cerberus-${timestamp}"
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
changed=0
trap - EXIT
cleanup_staged
echo "Installed hostname ${role} and loopback identity; backup: ${backup}"
