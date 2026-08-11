#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT
config="${test_root}/avahi-daemon.conf"
fake_bin="${test_root}/bin"
mkdir -p "${fake_bin}"

printf '%s\n' \
  '[server]' \
  'use-ipv4=yes' \
  '#allow-interfaces=eth0' \
  '#deny-interfaces=eth1' >"${config}"
printf '%s\n' '#!/usr/bin/env bash' 'printf "cerberus2\n"' >"${fake_bin}/hostname"
chmod 0755 "${fake_bin}/hostname"

output="$(
  PATH="${fake_bin}:${PATH}" AVAHI_CONFIG_FILE="${config}" \
    "${repo_root}/scripts/install-cerberus-mdns.sh" verify
)"
grep -Fq -- '-#allow-interfaces=eth0' <<<"${output}"
grep -Fq -- '+allow-interfaces=enP7s7' <<<"${output}"
grep -Fq 'no change made' <<<"${output}"
grep -Fxq '#allow-interfaces=eth0' "${config}"

set +e
apply_output="$(
  PATH="${fake_bin}:${PATH}" AVAHI_CONFIG_FILE="${config}" \
    "${repo_root}/scripts/install-cerberus-mdns.sh" apply 2>&1
)"
apply_status=$?
set -e
[[ "${apply_status}" == 2 ]]
grep -Fq 'overrides are allowed only for verify' <<<"${apply_output}"

echo 'Cerberus management-only mDNS test passed.'
