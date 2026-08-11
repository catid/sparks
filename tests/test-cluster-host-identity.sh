#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_root="$(mktemp -d)"
trap 'rm -rf -- "${test_root}"' EXIT

hosts_fixture="${test_root}/hosts"
cat >"${hosts_fixture}" <<'EOF'
127.0.0.1 localhost
127.0.1.1 cerebrus1 workstation-alias
192.0.2.11 cerebrus1.lan cerebrus1 spark1.lan spark1
192.0.2.14 cerberus1.local cerebrus2.local spark3.local
192.0.2.12 unrelated.example unrelated
# BEGIN SPARKS CLUSTER HOSTS
192.0.2.13 cerberus3.lan cerberus3
# END SPARKS CLUSTER HOSTS
::1 localhost ip6-localhost ip6-loopback
EOF

for requested_role in cerberus1 cerebrus1 spark1; do
  output="$(
    CLUSTER_HOSTS_FILE="${hosts_fixture}" \
      "${repo_root}/scripts/install-cluster-host-identity.sh" \
        "${requested_role}"
  )"
  grep -Fq 'Validated canonical role cerberus1' <<<"${output}"
  grep -Fq '+127.0.1.1 cerberus1 workstation-alias' <<<"${output}"
  grep -Fq -- '-192.0.2.11 cerebrus1.lan cerebrus1 spark1.lan spark1' \
    <<<"${output}"
  grep -Fq -- '-192.0.2.14 cerberus1.local cerebrus2.local spark3.local' \
    <<<"${output}"
  grep -Fq -- '-192.0.2.13 cerberus3.lan cerberus3' <<<"${output}"
  if grep -Fq -- '-192.0.2.12 unrelated.example unrelated' <<<"${output}"; then
    echo 'Identity preview unexpectedly removed an unrelated host entry.' >&2
    exit 1
  fi
done

for suffix in 1 2 3; do
  fragment="${repo_root}/hosts/cerberus${suffix}.hosts"
  grep -Fxq "127.0.1.1 cerberus${suffix}" "${fragment}"
  if grep -Eq '(^|[[:space:]])(cerberus|cerebrus|spark)[123](\.(lan|local))?([[:space:]]|$)' \
      < <(grep -v -Fx "127.0.1.1 cerberus${suffix}" "${fragment}"); then
    echo "Unexpected peer alias in ${fragment}." >&2
    exit 1
  fi
  if grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}[[:space:]]' \
      < <(grep -v '^127\.' "${fragment}"); then
    echo "Unexpected management address in ${fragment}." >&2
    exit 1
  fi
done

ssh_config="${repo_root}/ssh/cluster.config.example"
if grep -Eq '^[[:space:]]*HostName[[:space:]]+([0-9]{1,3}\.){3}[0-9]{1,3}$' \
    "${ssh_config}"; then
  echo 'Cluster SSH example pins a numeric management address.' >&2
  exit 1
fi
for suffix in 1 2 3; do
  grep -Eq "^Host .*cerberus${suffix} .*cerebrus${suffix} .*spark${suffix}" \
    "${ssh_config}"
  grep -Fxq "    HostName cerberus${suffix}.local" "${ssh_config}"
  for alias in "cerberus${suffix}" "cerebrus${suffix}" "spark${suffix}"; do
    resolved_host="$(
      ssh -G -F "${ssh_config}" "${alias}" 2>/dev/null |
        awk '$1 == "hostname" { print $2; exit }'
    )"
    [[ "${resolved_host}" == "cerberus${suffix}.local" ]]
  done
done

set +e
apply_output="$(
  CLUSTER_HOSTS_FILE="${hosts_fixture}" \
    "${repo_root}/scripts/install-cluster-host-identity.sh" cerberus1 --apply \
      2>&1
)"
apply_status=$?
set -e
[[ "${apply_status}" == 2 ]]
grep -Fq 'overrides are allowed only for a dry run' <<<"${apply_output}"

echo 'Cluster host identity canonicalization tests passed.'
