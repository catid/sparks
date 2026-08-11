#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fixture="${repo_root}/tests/fixtures/shared-cluster-key/fake-command"
test_root="$(mktemp -d)"
cleanup() {
  chmod -R u+rwx -- "${test_root}" 2>/dev/null || true
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

fake_bin="${test_root}/bin"
key="${test_root}/id_ed25519_dgx_cluster"
known_hosts="${test_root}/dgx_cluster_known_hosts"
log="${test_root}/commands.log"
mkdir -p -- "${fake_bin}"
for command in getent hostname ip scp ssh; do
  ln -s -- "${fixture}" "${fake_bin}/${command}"
done
ssh-keygen -q -t ed25519 -N '' -C test-only -f "${key}"
chmod 0600 -- "${key}"
install -m 0600 /dev/null "${known_hosts}"
install -m 0600 /dev/null "${log}"

output="$(
  TEST_CLUSTER_KEY_LOG="${log}" \
  CLUSTER_SSH_KEY="${key}" \
  CLUSTER_SSH_KNOWN_HOSTS="${known_hosts}" \
  PATH="${fake_bin}:/usr/bin:/bin" \
    "${repo_root}/scripts/install-shared-cluster-key.sh" --install \
      cerebrus1.lan cerebrus2
)"

grep -Fq 'Retained existing cluster identity for local target cerebrus1.lan.' \
  <<<"${output}"
mapfile -t scp_calls < <(grep '^scp ' "${log}")
[[ "${#scp_calls[@]}" == 1 ]]
[[ "${scp_calls[0]}" == *'catid@cerebrus2:.ssh/'* ]]
[[ "${scp_calls[0]}" != *'cerebrus1'* ]]
ssh-keygen -y -f "${key}" >/dev/null

echo "Shared-cluster-key test passed: a local alias cannot overwrite the coordinator key."
