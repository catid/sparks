#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT
fake_bin="${test_root}/bin"
state="${test_root}/state"
mkdir -p "${fake_bin}" "${state}"

printf '%s\n' '#!/usr/bin/env bash' 'printf "cerberus2\n"' \
  >"${fake_bin}/hostname"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' >"${fake_bin}/ip"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "${1:-}" == -n ]]; then shift; fi' \
  'exec "$@"' >"${fake_bin}/sudo"
cat >"${fake_bin}/nmcli" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
state="${NMCLI_TEST_STATE:?}"
printf '%q ' "$@" >>"${state}/calls"
printf '\n' >>"${state}/calls"
if [[ "${1:-}" == -g ]]; then
  property="$2"
  [[ -f "${state}/exists" ]] || exit 10
  case "${property}" in
    connection.uuid) printf 'fixture-uuid\n' ;;
    connection.type) printf '802-3-ethernet\n' ;;
    connection.interface-name) printf 'enP7s7\n' ;;
    connection.autoconnect) printf 'yes\n' ;;
    connection.autoconnect-priority) printf '100\n' ;;
    ipv4.method) printf 'auto\n' ;;
    ipv4.addresses|ipv4.gateway) printf '\n' ;;
    ipv4.dhcp-hostname|ipv6.dhcp-hostname) printf 'cerberus2\n' ;;
    ipv4.dhcp-send-hostname|ipv6.dhcp-send-hostname) printf 'yes\n' ;;
    ipv4.ignore-auto-dns) printf 'no\n' ;;
    *) exit 11 ;;
  esac
  exit 0
fi
case " $* " in
  *' connection add '*) touch "${state}/exists" ;;
esac
exit 0
EOF
chmod 0755 "${fake_bin}"/*

set +e
missing_output="$(
  PATH="${fake_bin}:${PATH}" NMCLI_TEST_STATE="${state}" \
    "${repo_root}/scripts/install-cerberus-management.sh" verify 2>&1
)"
missing_status=$?
set -e
[[ "${missing_status}" == 1 ]]
grep -Fq 'Missing NetworkManager profile: cerberus-mgmt' <<<"${missing_output}"

apply_output="$(
  PATH="${fake_bin}:${PATH}" NMCLI_TEST_STATE="${state}" \
    "${repo_root}/scripts/install-cerberus-management.sh" apply
)"
grep -Fq 'Installed cerberus-mgmt' <<<"${apply_output}"
grep -Fq 'connection add type ethernet ifname enP7s7 con-name cerberus-mgmt' \
  "${state}/calls"
grep -Fq 'ipv4.method auto' "${state}/calls"
grep -Fq 'ipv4.dhcp-hostname cerberus2' "${state}/calls"
grep -Fq 'ipv6.dhcp-hostname cerberus2' "${state}/calls"
grep -Fq 'ipv6.dhcp-send-hostname yes' "${state}/calls"
if grep -Eq '10\.10\.|192\.168\.' "${state}/calls"; then
  echo 'Management profile persisted a numeric cluster address.' >&2
  exit 1
fi

before_calls="$(wc -l <"${state}/calls")"
verify_output="$(
  PATH="${fake_bin}:${PATH}" NMCLI_TEST_STATE="${state}" \
    "${repo_root}/scripts/install-cerberus-management.sh" verify
)"
grep -Fq 'Verified cerberus-mgmt: DHCP on enP7s7, hostname cerberus2.' \
  <<<"${verify_output}"

idempotent_output="$(
  PATH="${fake_bin}:${PATH}" NMCLI_TEST_STATE="${state}" \
    "${repo_root}/scripts/install-cerberus-management.sh" apply
)"
grep -Fq 'already canonical' <<<"${idempotent_output}"
if tail -n "+$((before_calls + 1))" "${state}/calls" |
    grep -Eq 'connection (add|clone|modify)'; then
  echo 'Idempotent apply unexpectedly rewrote the management profile.' >&2
  exit 1
fi

echo 'Cerberus persistent DHCP management-profile tests passed.'
