#!/usr/bin/env bash

set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
action="${1:-verify}"
target="/usr/local/libexec/dgx-spark-dashboard-probe"

usage() {
  cat <<'EOF'
Usage: install-dashboard-probe.sh [verify|install]

verify   Validate the fixed read-only probe without changing the Spark.
install  Install it root-owned at /usr/local/libexec/dgx-spark-dashboard-probe.

After installing, restrict the collector's public key in authorized_keys:

  restrict,command="/usr/local/libexec/dgx-spark-dashboard-probe" ssh-ed25519 ...

Do not paste a private key into this repository or this command.
EOF
}

case "${action}" in
  verify|install) ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

probe="${root_dir}/dashboard/remote-probe.sh"
[[ -f "${probe}" && ! -L "${probe}" ]] || {
  echo "Probe must be a regular, non-symlink file: ${probe}" >&2
  exit 2
}
/bin/sh -n "${probe}"

if grep -Eq \
  '^[[:space:]]*(eval|exec|sh|bash).*(SSH_ORIGINAL_COMMAND|sh -s)' \
  "${probe}"; then
  echo "Probe contains a forbidden dynamic-command construct." >&2
  exit 2
fi

if [[ "${action}" == "verify" ]]; then
  echo "Verified fixed dashboard SSH probe."
  exit 0
fi

sudo install -d -o root -g root -m 0755 "$(dirname "${target}")"
sudo install -o root -g root -m 0755 "${probe}" "${target}"
echo "Installed ${target}."
echo "Now force the dashboard public key to this command in authorized_keys."
