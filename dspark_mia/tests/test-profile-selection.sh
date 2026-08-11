#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_profile="${MIA_ENV_FILE:-mia-throughput.env}"
test_profile_basename="$(basename -- "${test_profile}")"

output="$(
  MIA_ENV_FILE="${test_profile}" \
    "${root}/bin/validate-static.sh"
)"
grep -Fq "profile_file=${test_profile_basename} project=mia-dspark-throughput" <<<"${output}"
grep -Fq 'API=8889 master=29631' <<<"${output}"
grep -Fq 'direct-edge' <<<"${output}"
grep -Fq 'max_num_seqs=32 max_batched_tokens=8192 capture=192 gpu_util=0.78' <<<"${output}"

set +e
outside_output="$(
  MIA_ENV_FILE=/etc/environment \
    "${root}/bin/validate-static.sh" 2>&1
)"
outside_status=$?
set -e
[[ "${outside_status}" == "2" ]] || {
  echo "Expected outside-root profile rejection status 2, got ${outside_status}." >&2
  exit 1
}
grep -Fq "Profile must be directly inside ${root}" <<<"${outside_output}"

remote_path="$(
  MIA_ENV_FILE="${test_profile}" bash -c \
    'source "$1"; remote_profile_path' _ "${root}/bin/common.sh"
)"
[[ "${remote_path}" == "${root}/${test_profile_basename}" ]]

grep -Fq 'remote_profile_assignment' "${root}/bin/preflight.sh"
for script in start.sh preflight.sh; do
  grep -Fq 'remote_runtime_assignment' "${root}/bin/${script}"
done
for script in status.sh stop.sh; do
  grep -Fq 'remote_nonlaunch_assignment' "${root}/bin/${script}"
done
grep -Fq 'load_tp2_runtime_addresses' "${root}/bin/start.sh"
grep -Fq 'resolve_tp2_runtime_addresses' "${root}/bin/resolve-runtime.sh"
grep -Fq 'remote_readiness_helper' "${root}/bin/sync-worker.sh"
grep -Fq 'local_readiness_sha' "${root}/bin/sync-worker.sh"

echo "Profile-selection test passed: seq32 values, containment, hostname runtime resolution, remote propagation, and rail-gate hashing are explicit."
