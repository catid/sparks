#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
test_profile="${MIA_ENV_FILE:-mia-throughput.env}"
supervisor="${root}/bin/supervise.sh"
fixture_dir="${root}/tests/fixtures/supervisor"
fixture_path="${fixture_dir}/path"
test_root="$(mktemp -d)"
background_pid=""

cleanup() {
  if [[ -n "${background_pid}" ]] && kill -0 "${background_pid}" 2>/dev/null; then
    kill -TERM "${background_pid}" 2>/dev/null || true
    wait "${background_pid}" 2>/dev/null || true
  fi
  rm -rf -- "${test_root}"
}
trap cleanup EXIT

new_case() {
  local name="$1"
  case_dir="${test_root}/${name}"
  mkdir -p "${case_dir}/state" "${case_dir}/runtime"
  : >"${case_dir}/events.log"
  : >"${case_dir}/probe.states"
  printf '%s\n' 0 >"${case_dir}/start.statuses"
}

run_supervisor() {
  local checks="$1"
  shift
  env \
    PATH="${fixture_path}:${PATH}" \
    MIA_ENV_FILE="${test_profile}" \
    MIA_SUPERVISOR_HELPER_DIR="${fixture_dir}" \
    MIA_SUPERVISOR_STATE_DIR="${case_dir}/state" \
    MIA_SUPERVISOR_RUNTIME_DIR="${case_dir}/runtime" \
    MIA_SUPERVISOR_TEST_DIR="${case_dir}" \
    MIA_SUPERVISOR_POLL_SECONDS=1 \
    MIA_SUPERVISOR_SSH_FAILURE_THRESHOLD=2 \
    MIA_SUPERVISOR_DEGRADED_SSH_FAILURE_THRESHOLD=4 \
    MIA_SUPERVISOR_API_FAILURE_THRESHOLD=2 \
    MIA_SUPERVISOR_BACKOFF_INITIAL_SECONDS=2 \
    MIA_SUPERVISOR_BACKOFF_MAX_SECONDS=5 \
    MIA_SUPERVISOR_STABLE_CHECKS_FOR_RESET=2 \
    MIA_SUPERVISOR_PROBE_TIMEOUT_SECONDS=1 \
    MIA_SUPERVISOR_STOP_TIMEOUT_SECONDS=5 \
    MIA_SUPERVISOR_START_TIMEOUT_SECONDS=5 \
    MIA_SUPERVISOR_MAX_CHECKS="${checks}" \
    "$@" \
    "${supervisor}"
}

lifecycle_events() {
  grep -E '^(STOP|START):' "${case_dir}/events.log" || true
}

assert_lifecycle() {
  local expected="$1"
  local actual
  actual="$(lifecycle_events)"
  if [[ "${actual}" != "${expected}" ]]; then
    printf 'Unexpected lifecycle events in %s\nexpected:\n%s\nactual:\n%s\n' \
      "${case_dir}" "${expected}" "${actual}" >&2
    exit 1
  fi
}

# A supervisor starting over an already healthy pair adopts its exact
# generation fingerprint and does not disturb either rank.
new_case healthy-adoption
printf '%s\n' '0|generation-A|healthy' >"${case_dir}/probe.states"
run_supervisor 1 >"${case_dir}/supervisor.log" 2>&1
[[ "$(<"${case_dir}/state/epoch")" == "generation-A" ]]
assert_lifecycle ""
grep -Fq 'Adopted the existing healthy TP2 generation.' \
  "${case_dir}/supervisor.log"

# A hard rank failure has threshold one. Recovery must stop the complete old
# pair before starting a fresh worker-first generation.
new_case immediate-rank-recovery
printf '%s\n' \
  '10||local rank is gone' \
  '0|generation-B|healthy after restart' >"${case_dir}/probe.states"
run_supervisor 1 >"${case_dir}/supervisor.log" 2>&1
assert_lifecycle $'STOP:both\nSTART:0'
[[ "$(<"${case_dir}/state/epoch")" == "generation-B" ]]
grep -Fq 'Health failure 1/1 (status 10): local rank is gone' \
  "${case_dir}/supervisor.log"
grep -Fq 'Recovery complete;' "${case_dir}/supervisor.log"

# Even a nominally healthy pair must be replaced if either boot/container
# identity differs from the supervisor-owned epoch.
new_case epoch-mismatch
printf '%s\n' 'generation-old' >"${case_dir}/state/epoch"
printf '%s\n' \
  '0|generation-out-of-band|containers look healthy' \
  '0|generation-coordinated|healthy after restart' >"${case_dir}/probe.states"
run_supervisor 1 >"${case_dir}/supervisor.log" 2>&1
assert_lifecycle $'STOP:both\nSTART:0'
[[ "$(<"${case_dir}/state/epoch")" == "generation-coordinated" ]]
grep -Fq 'rank generation identity changed outside coordinated recovery' \
  "${case_dir}/supervisor.log"

# API failures are debounced. One failure only polls; the second consecutive
# failure triggers exactly one pair recovery.
new_case api-threshold
printf '%s\n' \
  '13||API timeout one' \
  '13||API timeout two' \
  '0|generation-api-recovered|healthy after restart' >"${case_dir}/probe.states"
run_supervisor 2 >"${case_dir}/supervisor.log" 2>&1
assert_lifecycle $'STOP:both\nSTART:0'
grep -Fq 'Health failure 1/2 (status 13): API timeout one' \
  "${case_dir}/supervisor.log"
grep -Fq 'Health failure 2/2 (status 13): API timeout two' \
  "${case_dir}/supervisor.log"
[[ "$(grep -Fxc 'SLEEP:1' "${case_dir}/events.log")" == "1" ]]

# Failed launches remain inside one serialized recovery and use capped
# exponential backoff before eventually publishing the new epoch.
new_case retry-backoff
printf '%s\n' \
  '10||rank exited' \
  '0|generation-after-retries|healthy after retries' >"${case_dir}/probe.states"
printf '%s\n' 1 1 1 0 >"${case_dir}/start.statuses"
run_supervisor 1 >"${case_dir}/supervisor.log" 2>&1
assert_lifecycle $'STOP:both\nSTART:1\nSTOP:both\nSTART:1\nSTOP:both\nSTART:1\nSTOP:both\nSTART:0'
[[ "$(grep -Ec '^SLEEP:(2|4|5)$' "${case_dir}/events.log")" == "3" ]]
grep -Fq 'SLEEP:2' "${case_dir}/events.log"
grep -Fq 'SLEEP:4' "${case_dir}/events.log"
grep -Fq 'SLEEP:5' "${case_dir}/events.log"
[[ "$(<"${case_dir}/state/epoch")" == "generation-after-retries" ]]

# Different transient failure classes cannot reset one another forever. The
# cumulative degraded window reaches recovery even though statuses alternate.
new_case alternating-soft-failures
printf '%s\n' \
  '13||API soft failure one' \
  '16||remote SSH degraded while API is live' \
  '13||API soft failure two' \
  '0|generation-after-alternation|healthy after restart' >"${case_dir}/probe.states"
run_supervisor 3 >"${case_dir}/supervisor.log" 2>&1
assert_lifecycle $'STOP:both\nSTART:0'
grep -Fq 'Health failure 1/2 (status 13): API soft failure one' \
  "${case_dir}/supervisor.log"
grep -Fq 'Health failure 2/4 (status 16): remote SSH degraded while API is live' \
  "${case_dir}/supervisor.log"
grep -Fq 'Health failure 3/2 (status 13): API soft failure two' \
  "${case_dir}/supervisor.log"
[[ "$(<"${case_dir}/state/epoch")" == "generation-after-alternation" ]]

# GNU timeout must turn a wedged probe into the supervisor's bounded status 15.
# Two consecutive timeouts meet the default catch-all threshold and recover.
new_case hung-probe
printf '%s\n' \
  'HANG||first intentional probe hang' \
  'HANG||second intentional probe hang' \
  '0|generation-after-hang|healthy after restart' >"${case_dir}/probe.states"
run_supervisor 2 >"${case_dir}/supervisor.log" 2>&1
assert_lifecycle $'STOP:both\nSTART:0'
grep -Fq \
  'Health failure 1/2 (status 15): health probe exceeded its 1-second wall-clock limit' \
  "${case_dir}/supervisor.log"
grep -Fq \
  'Health failure 2/2 (status 15): health probe exceeded its 1-second wall-clock limit' \
  "${case_dir}/supervisor.log"
[[ "$(grep -c '^PROBE:HANG:' "${case_dir}/events.log")" == "2" ]]
[[ "$(<"${case_dir}/state/epoch")" == "generation-after-hang" ]]

# A second process sharing the runtime directory must fail at the supervisor
# lock before it can probe or issue a lifecycle operation.
new_case duplicate-lock
printf '%s\n' '0|generation-lock-owner|healthy' >"${case_dir}/probe.states"
ready="${case_dir}/sleep.ready"
release="${case_dir}/sleep.release"
env \
  PATH="${fixture_path}:${PATH}" \
  MIA_ENV_FILE="${test_profile}" \
  MIA_SUPERVISOR_HELPER_DIR="${fixture_dir}" \
  MIA_SUPERVISOR_STATE_DIR="${case_dir}/state" \
  MIA_SUPERVISOR_RUNTIME_DIR="${case_dir}/runtime" \
  MIA_SUPERVISOR_TEST_DIR="${case_dir}" \
  MIA_SUPERVISOR_TEST_SLEEP_READY="${ready}" \
  MIA_SUPERVISOR_TEST_SLEEP_RELEASE="${release}" \
  MIA_SUPERVISOR_POLL_SECONDS=1 \
  MIA_SUPERVISOR_SSH_FAILURE_THRESHOLD=2 \
  MIA_SUPERVISOR_DEGRADED_SSH_FAILURE_THRESHOLD=4 \
  MIA_SUPERVISOR_API_FAILURE_THRESHOLD=2 \
  MIA_SUPERVISOR_BACKOFF_INITIAL_SECONDS=2 \
  MIA_SUPERVISOR_BACKOFF_MAX_SECONDS=5 \
  MIA_SUPERVISOR_STABLE_CHECKS_FOR_RESET=2 \
  MIA_SUPERVISOR_PROBE_TIMEOUT_SECONDS=1 \
  MIA_SUPERVISOR_STOP_TIMEOUT_SECONDS=5 \
  MIA_SUPERVISOR_START_TIMEOUT_SECONDS=5 \
  MIA_SUPERVISOR_MAX_CHECKS=0 \
  "${supervisor}" >"${case_dir}/owner.log" 2>&1 &
background_pid=$!

for _ in $(seq 1 100); do
  [[ -e "${ready}" ]] && break
  /usr/bin/sleep 0.02
done
[[ -e "${ready}" ]] || {
  echo "Timed out waiting for the lock owner to enter polling sleep." >&2
  exit 1
}

set +e
run_supervisor 1 >"${case_dir}/contender.log" 2>&1
contender_status=$?
set -e
[[ "${contender_status}" == "75" ]] || {
  echo "Expected duplicate supervisor status 75, got ${contender_status}." >&2
  exit 1
}
grep -Fq 'Another DSpark supervisor already owns' \
  "${case_dir}/contender.log"
assert_lifecycle ""
[[ "$(<"${case_dir}/probe.index")" == "1" ]]

kill -TERM "${background_pid}"
wait "${background_pid}"
background_pid=""
assert_lifecycle 'STOP:both'
grep -Fq 'Supervisor shutdown requested; cleaning both scoped ranks.' \
  "${case_dir}/owner.log"

# Direct lifecycle commands use the same supervisor/recovery lock pair. Hold
# the supervisor lock and invoke both public commands; harmless PATH/helper
# fixtures prevent Docker or SSH access even if a future regression gets past
# the lock.
new_case direct-lifecycle-lock
exec {lifecycle_lock_fd}>"${case_dir}/runtime/supervisor.lock"
flock -n "${lifecycle_lock_fd}"
for lifecycle_command in start stop; do
  set +e
  lifecycle_output="$(
    env \
      PATH="${fixture_dir}/lifecycle-path:${root}/tests/fixtures/path:${PATH}" \
      MIA_ENV_FILE="${test_profile}" \
      MIA_SUPERVISOR_RUNTIME_DIR="${case_dir}/runtime" \
      MIA_SUPERVISOR_TEST_DIR="${case_dir}" \
      MIA_START_HELPER_DIR="${fixture_dir}" \
      MIA_START_TEST_LOG="${case_dir}/events.log" \
      "${root}/bin/${lifecycle_command}.sh" 2>&1
  )"
  lifecycle_status=$?
  set -e
  [[ "${lifecycle_status}" == "75" ]] || {
    echo "Expected direct ${lifecycle_command} lock status 75, got ${lifecycle_status}." >&2
    exit 1
  }
  grep -Fq 'The DSpark supervisor is active; use systemctl for lifecycle changes.' \
    <<<"${lifecycle_output}"
done

# ExecStopPost-style cleanup must not erase ownership when a live supervisor
# rejects its lifecycle attempt.
printf '%s\n' 4242 >"${case_dir}/state/owner-active"
set +e
owned_stop_output="$(
  env \
    PATH="${fixture_dir}/lifecycle-path:${root}/tests/fixtures/path:${PATH}" \
    MIA_ENV_FILE="${test_profile}" \
    MIA_SUPERVISOR_STATE_DIR="${case_dir}/state" \
    MIA_SUPERVISOR_RUNTIME_DIR="${case_dir}/runtime" \
    MIA_SUPERVISOR_TEST_DIR="${case_dir}" \
    "${root}/bin/stop-if-owned.sh" 2>&1
)"
owned_stop_status=$?
set -e
[[ "${owned_stop_status}" == "75" ]] || {
  echo "Expected owned cleanup lock status 75, got ${owned_stop_status}." >&2
  exit 1
}
[[ "$(<"${case_dir}/state/owner-active")" == "4242" ]]
grep -Fq 'The DSpark supervisor is active; use systemctl for lifecycle changes.' \
  <<<"${owned_stop_output}"

flock -u "${lifecycle_lock_fd}"
exec {lifecycle_lock_fd}>&-
grep -Fxq 'acquire_lifecycle_locks' "${root}/bin/start.sh"
grep -Fxq 'acquire_lifecycle_locks' "${root}/bin/stop.sh"
assert_lifecycle ""
if grep -Fq 'UNEXPECTED:' "${case_dir}/events.log"; then
  echo "A direct lifecycle command passed its supervisor lock." >&2
  exit 1
fi

grep -Fq 'poll_seconds="${MIA_SUPERVISOR_POLL_SECONDS:-30}"' \
  "${root}/bin/supervise.sh"
grep -Fq 'ControlMaster=auto' "${root}/bin/probe.sh"
grep -Fq 'ControlPersist=90' "${root}/bin/probe.sh"
grep -Fq 'MIA_SUPERVISOR_POLL_SECONDS=30' \
  "${root}/../systemd/dgx-spark-dspark-mia.service.in"
grep -Fq 'MIA_HEALTH_FULL_PROBE_INTERVAL_SECONDS=30' \
  "${root}/../systemd/dgx-spark-dspark-mia.service.in"
grep -Fq 'MIA_SSH_CONTROL_DIR=/run/dgx-spark-dspark-mia/ssh' \
  "${root}/../systemd/dgx-spark-dspark-mia.service.in"
grep -Fq 'ssh_control_dir="${MIA_SUPERVISOR_RUNTIME_DIR}/ssh"' \
  "${root}/bin/probe.sh"
grep -Fq 'MIA_HEALTH_FULL_PROBE_INTERVAL_SECONDS:-30' \
  "${root}/bin/probe.sh"
grep -Fq 'rm -f -- "${MIA_SUPERVISOR_RUNTIME_DIR}/last-full-probe"' \
  "${root}/bin/stop.sh"

echo "Supervisor tests passed: adoption, coordinated recovery, epoch identity, soft-failure accumulation, probe timeout, retry backoff, and lifecycle locking."
