#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_profile="${MIA_ENV_FILE:-mia-throughput.env}"
case "${test_profile}" in
  /*) test_profile_path="$(readlink -f -- "${test_profile}")" ;;
  *) test_profile_path="$(readlink -f -- "${root}/${test_profile}")" ;;
esac
test_profile_basename="$(basename -- "${test_profile_path}")"
test_root="$(mktemp -d)"
test_log="${test_root}/start.log"
: >"${test_log}"
cleanup() {
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

set +e
PATH="${root}/tests/fixtures/path:${PATH}" \
MIA_ENV_FILE="${test_profile_path}" \
MIA_START_HELPER_DIR="${root}/tests/fixtures/helpers" \
MIA_START_TEST_LOG="${test_log}" \
MIA_SUPERVISOR_LOCK_DIR="${test_root}/locks" \
MIA_WAIT_ATTEMPTS=1 \
MIA_WAIT_SECONDS=0 \
  "${root}/bin/start.sh" >/dev/null 2>&1
status=$?
set -e

[[ "${status}" == "1" ]] || {
  echo "Expected timeout exit 1, got ${status}." >&2
  exit 1
}
grep -Fqx 'node-compose 0 up -d --no-build --pull never' "${test_log}"
grep -Fqx 'node-compose 0 down --timeout 30' "${test_log}"
grep -Fqx "node-compose-profile=${test_profile_basename}" "${test_log}"
grep -Fq 'node-compose.sh' "${test_log}"
grep -Fq "MIA_ENV_FILE=${test_profile_path}" "${test_log}"
grep -Fq '\ 1\ up\ -d\ --no-build\ --pull\ never' "${test_log}"
grep -Fq '\ 1\ down\ --timeout\ 30' "${test_log}"
grep -Fqx 'ranks-running' "${test_log}"

echo "Timeout rollback test passed: throughput profile reached and tore down both isolated ranks."
