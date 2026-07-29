#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
rank="${1:-}"
apply="${2:-}"
target="/etc/netplan/40-cx7.yaml"

usage() {
  cat <<'EOF'
Usage: install-cx7-netplan.sh spark1|spark2 [--apply]

Without --apply, validates the selected repository file in an isolated
temporary Netplan root and prints the proposed change. With --apply, backs up
the exact target, installs the file, runs netplan generate, and applies it.

The management 10GbE interface is not changed, but retain console access when
changing networking on a remote machine.
EOF
}

case "${rank}" in
  spark1) source_file="${root_dir}/netplan/spark1-40-cx7.yaml" ;;
  spark2) source_file="${root_dir}/netplan/spark2-40-cx7.yaml" ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [[ -n "${apply}" && "${apply}" != "--apply" ]]; then
  usage >&2
  exit 2
fi

expected_host="${rank}"
actual_host="$(hostname -s)"
if [[ -n "${apply}" && "${actual_host}" != "${expected_host}" ]]; then
  echo "Refusing to install ${rank} networking on host ${actual_host}." >&2
  exit 2
fi

for interface in \
  enp1s0f0np0 enP2p1s0f0np0 enp1s0f1np1 enP2p1s0f1np1; do
  if [[ ! -d "/sys/class/net/${interface}" ]]; then
    echo "Required ConnectX-7 netdev is absent: ${interface}" >&2
    exit 1
  fi
done

validation_root="$(mktemp -d)"
trap 'rm -rf -- "${validation_root}"' EXIT
install -d -m 0755 "${validation_root}/etc/netplan"
install -m 0600 "${source_file}" "${validation_root}/etc/netplan/40-cx7.yaml"
sudo netplan generate --root-dir "${validation_root}"

echo "Validated ${source_file}."
if [[ -z "${apply}" ]]; then
  echo "Dry run only; rerun with --apply to install ${target}."
  exit 0
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
if sudo test -e "${target}"; then
  sudo cp -a -- "${target}" "${target}.before-sparks-${timestamp}"
fi
sudo install -o root -g root -m 0600 "${source_file}" "${target}"
sudo netplan generate
sudo netplan apply
"${root_dir}/bin/wait-cx7-ready.sh" --check-once
echo "Installed and verified ${target} for ${rank}."
