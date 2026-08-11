#!/usr/bin/env bash
set -euo pipefail

integration_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(cd "${integration_root}/.." && pwd -P)"
fixture_path="${integration_root}/tests/fixtures/runtime-path"
profile_basename="mia-runtime-resolution-test-${$}.env"
profile_path="${integration_root}/${profile_basename}"
test_root="$(mktemp -d)"
test_key="${test_root}/cluster-key"

cleanup() {
  rm -f -- "${profile_path}"
  rm -rf -- "${test_root}"
}
trap cleanup EXIT
: >"${test_key}"
chmod 0600 "${test_key}"

CLUSTER_SSH_KEY="${test_key}" \
DSPARK_PROFILE_NAME="${profile_basename}" \
  "${repo_root}/scripts/configure-dspark-profile.sh" --profile agent >/dev/null

runtime_output="$({
  PATH="${fixture_path}:${PATH}" \
  MIA_ENV_FILE="${profile_basename}" \
    bash -c 'source "$1"; resolve_tp2_runtime_addresses' \
      _ "${integration_root}/bin/common.sh"
} 2>"${test_root}/runtime.log")"
[[ "${runtime_output}" == $'198.51.100.10\n198.51.100.10\n198.51.100.11' ]]
grep -Fq 'Resolved TP2 management plane from hostnames' "${test_root}/runtime.log"

candidates="$(
  PATH="${fixture_path}:${PATH}" \
  MIA_ENV_FILE="${profile_basename}" \
    bash -c 'source "$1"; management_dns_candidates cerberus1' \
      _ "${integration_root}/bin/common.sh"
)"
[[ "${candidates}" == $'cerberus1.local\ncerberus1\ncerberus1.lan\nspark1.lan\ncerebrus1.lan' ]]

remote_assignment="$(
  MIA_ENV_FILE="${profile_basename}" \
  MASTER_ADDR=198.51.100.10 \
  VLLM_HOST_IP=198.51.100.10 \
  WORKER_VLLM_HOST_IP=198.51.100.11 \
    bash -c 'source "$1"; remote_runtime_assignment' \
      _ "${integration_root}/bin/common.sh"
)"
grep -Fq 'MASTER_ADDR=198.51.100.10' <<<"${remote_assignment}"
grep -Fq 'VLLM_HOST_IP=198.51.100.11' <<<"${remote_assignment}"

for scenario in worker_route_wrong worker_dns_mismatch remote_route_wrong remote_host_wrong; do
  set +e
  failure_output="$(
    PATH="${fixture_path}:${PATH}" \
    MIA_RUNTIME_FIXTURE_SCENARIO="${scenario}" \
    MIA_ENV_FILE="${profile_basename}" \
      bash -c 'source "$1"; resolve_tp2_runtime_addresses' \
        _ "${integration_root}/bin/common.sh" 2>&1
  )"
  failure_status=$?
  set -e
  [[ "${failure_status}" != "0" ]] || {
    echo "Runtime resolver accepted invalid fixture scenario: ${scenario}" >&2
    exit 1
  }
  [[ -n "${failure_output}" ]]
done

echo "Runtime-resolution tests passed: canonical mDNS, SSH/legacy fallback, enP7s7 routing, and remote identity checks are enforced."
