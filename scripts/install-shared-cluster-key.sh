#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Usage: install-shared-cluster-key.sh --install HOST [HOST ...]
       install-shared-cluster-key.sh --verify HOST [HOST ...]

Copy the dedicated DGX cluster keypair from the current host to
already-authorized peers, or verify passwordless access among those peers.
This deliberately shares a private key, so use a cluster-only key rather than
a personal/laptop identity. Existing peer SSH configuration is never copied
or overwritten.

Environment:
  CLUSTER_SSH_KEY     default: ~/.ssh/id_ed25519_dgx_cluster
  CLUSTER_SSH_KNOWN_HOSTS  default: ~/.ssh/dgx_cluster_known_hosts
  CLUSTER_SSH_USER    default: current user
  CLUSTER_STRICT_HOST_KEY_CHECKING  default: yes; accept-new is opt-in
EOF
}

action="${1:-}"
shift || true
case "${action}" in
  --install|--verify) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
(( $# > 0 )) || { usage >&2; exit 2; }

cluster_key="${CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}"
cluster_pub="${cluster_key}.pub"
cluster_known_hosts="${CLUSTER_SSH_KNOWN_HOSTS:-${HOME}/.ssh/dgx_cluster_known_hosts}"
cluster_user="${CLUSTER_SSH_USER:-$(id -un)}"
strict_host_keys="${CLUSTER_STRICT_HOST_KEY_CHECKING:-yes}"
local_host="$(hostname -s)"

canonical_cluster_role() {
  case "$1" in
    cerberus1|cerberus1.local|cerberus1.lan|cerebrus1|cerebrus1.lan|spark1|spark1.lan) printf 'cerberus1\n' ;;
    cerberus2|cerberus2.local|cerberus2.lan|cerebrus2|cerebrus2.lan|spark2|spark2.lan) printf 'cerberus2\n' ;;
    cerberus3|cerberus3.local|cerberus3.lan|cerebrus3|cerebrus3.lan|spark3|spark3.lan) printf 'cerberus3\n' ;;
    *) return 1 ;;
  esac
}

is_local_target() {
  local target="$1" local_role="" target_role="" effective_host="" address local_address
  local -a local_addresses=()

  local_role="$(canonical_cluster_role "${local_host}" 2>/dev/null || true)"
  target_role="$(canonical_cluster_role "${target}" 2>/dev/null || true)"
  if [[ -n "${local_role}" && "${target_role}" == "${local_role}" ]]; then
    return 0
  fi
  [[ "${target}" == "${local_host}" ]] && return 0

  # Resolve SSH aliases through the effective HostName, then compare every
  # IPv4 result with addresses owned by this machine. This prevents an alias
  # for the coordinator from SCPing its only private key onto itself.
  effective_host="$(
    ssh -G "${target}" 2>/dev/null |
      awk '$1 == "hostname" { print $2; exit }'
  )"
  [[ -n "${effective_host}" ]] || effective_host="${target}"
  mapfile -t local_addresses < <(
    ip -4 -o address show | awk '{split($4, value, "/"); print value[1]}'
  )
  while IFS= read -r address; do
    for local_address in "${local_addresses[@]}"; do
      [[ "${address}" == "${local_address}" ]] && return 0
    done
  done < <(getent ahostsv4 "${effective_host}" 2>/dev/null | awk '{print $1}' | sort -u)
  return 1
}

[[ "${cluster_user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || {
  echo "Unsafe CLUSTER_SSH_USER: ${cluster_user}" >&2
  exit 2
}
for path in "${cluster_key}" "${cluster_pub}" "${cluster_known_hosts}"; do
  [[ -f "${path}" && ! -L "${path}" ]] || {
    echo "Missing regular cluster SSH file: ${path}" >&2
    exit 2
  }
done
for command in getent ip scp ssh ssh-keygen; do
  command -v "${command}" >/dev/null || {
    echo "Missing required command: ${command}" >&2
    exit 2
  }
done
[[ "$(basename -- "${cluster_key}")" == id_ed25519_dgx_cluster ]] || {
  echo "Cluster key basename must be id_ed25519_dgx_cluster." >&2
  exit 2
}
[[ "$(basename -- "${cluster_known_hosts}")" == dgx_cluster_known_hosts ]] || {
  echo "Cluster known-hosts basename must be dgx_cluster_known_hosts." >&2
  exit 2
}
[[ "${strict_host_keys}" == yes || "${strict_host_keys}" == accept-new ]] || {
  echo "CLUSTER_STRICT_HOST_KEY_CHECKING must be yes or accept-new." >&2
  exit 2
}
[[ "$(stat -c %a "${cluster_key}")" == "600" ]] || {
  echo "Private key must have mode 600: ${cluster_key}" >&2
  exit 2
}

read -r derived_type derived_body _ < <(ssh-keygen -y -f "${cluster_key}")
read -r public_type public_body _ <"${cluster_pub}"
[[ "${derived_type}" == "${public_type}" && \
   "${derived_body}" == "${public_body}" ]] || {
  echo "Private/public cluster key mismatch." >&2
  exit 2
}

ssh_options=(
  -i "${cluster_key}"
  -o IdentityAgent=none
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o "UserKnownHostsFile=${cluster_known_hosts}"
  -o "StrictHostKeyChecking=${strict_host_keys}"
)

for host in "$@"; do
  [[ "${host}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "Unsafe host: ${host}" >&2
    exit 2
  }
done

if [[ "${action}" == "--install" ]]; then
  for host in "$@"; do
    if is_local_target "${host}"; then
      echo "Retained existing cluster identity for local target ${host}."
      continue
    fi
    target="${cluster_user}@${host}"
    ssh "${ssh_options[@]}" "${target}" \
      'install -d -m 700 "$HOME/.ssh"'
    scp "${ssh_options[@]}" -p \
      "${cluster_key}" "${cluster_pub}" "${cluster_known_hosts}" \
      "${target}:.ssh/"
    ssh "${ssh_options[@]}" "${target}" \
      'chmod 700 "$HOME/.ssh" && chmod 600 "$HOME/.ssh/id_ed25519_dgx_cluster" "$HOME/.ssh/dgx_cluster_known_hosts" && chmod 644 "$HOME/.ssh/id_ed25519_dgx_cluster.pub"'
    echo "Installed shared cluster identity on ${host}."
  done
fi

# Test each directed path from this coordinator and from every named peer.
all_hosts=("$@")
for source in local "${all_hosts[@]}"; do
  for target in "${all_hosts[@]}"; do
    if [[ "${source}" == local ]]; then
      ssh "${ssh_options[@]}" "${cluster_user}@${target}" true
    else
      # Both values passed into the remote command were constrained above.
      # shellcheck disable=SC2029
      ssh "${ssh_options[@]}" "${cluster_user}@${source}" \
        "ssh -i ~/.ssh/id_ed25519_dgx_cluster -o IdentityAgent=none -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 -o UserKnownHostsFile=~/.ssh/dgx_cluster_known_hosts -o StrictHostKeyChecking=${strict_host_keys} ${cluster_user}@${target} true"
    fi
    echo "ssh ${source} -> ${target}: ok"
  done
done

ssh-keygen -lf "${cluster_pub}"
