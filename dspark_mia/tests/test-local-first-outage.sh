#!/usr/bin/env bash
set -euo pipefail

integration_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
repo_root="$(cd "${integration_root}/.." && pwd -P)"
fixture_root="${integration_root}/tests/fixtures/outage"
profile_basename="mia-outage-test-${$}.env"
profile_path="${integration_root}/${profile_basename}"
test_root="$(mktemp -d)"
test_key="${test_root}/cluster-key"
test_log="${test_root}/events.log"

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

: >"${test_log}"
set +e
status_output="$(
  PATH="${fixture_root}/path:${PATH}" \
  MIA_ENV_FILE="${profile_basename}" \
  MIA_STATUS_HELPER_DIR="${fixture_root}/helpers" \
  MIA_OUTAGE_TEST_LOG="${test_log}" \
    "${integration_root}/bin/status.sh" 2>&1
)"
status_code=$?
set -e
[[ "${status_code}" == "1" ]]
head_line="$(grep -nF '== cerberus1 / rank 0 ==' <<<"${status_output}" | cut -d: -f1)"
worker_line="$(grep -nF '== cerberus2 / rank 1 ==' <<<"${status_output}" | cut -d: -f1)"
[[ -n "${head_line}" && -n "${worker_line}" ]]
((head_line < worker_line))
grep -Fq 'fixture local compose: rank=0 command=ps' <<<"${status_output}"
grep -Fq 'head status above is still authoritative' <<<"${status_output}"
grep -Fq 'LOCAL 0 ps -a' "${test_log}"
grep -Fq 'SSH ' "${test_log}"

: >"${test_log}"
set +e
stop_output="$(
  PATH="${fixture_root}/path:${PATH}" \
  MIA_ENV_FILE="${profile_basename}" \
  MIA_STOP_HELPER_DIR="${fixture_root}/helpers" \
  MIA_SUPERVISOR_LOCK_DIR="${test_root}/locks" \
  MIA_OUTAGE_TEST_LOG="${test_log}" \
  MIA_STOP_COMMAND_TIMEOUT_SECONDS=5 \
    "${integration_root}/bin/stop.sh" 2>&1
)"
stop_code=$?
set -e
[[ "${stop_code}" == "1" ]]
grep -Fq 'LOCAL 0 down --timeout 30' "${test_log}"
grep -Fq 'SSH ' "${test_log}"
grep -Fq 'Pinned head project is down; worker cleanup is unconfirmed.' \
  <<<"${stop_output}"

echo "Local-first outage tests passed: C1 status and scoped cleanup survive worker DNS/SSH failure."
