#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command flock
need_command setsid
need_command timeout

helper_dir="${MIA_SUPERVISOR_HELPER_DIR:-${MIA_ROOT}/bin}"
state_dir="${MIA_SUPERVISOR_STATE_DIR:-${XDG_STATE_HOME:-/tmp}/dgx-spark-dspark-mia-${UID}}"
runtime_dir="${MIA_SUPERVISOR_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-/tmp}/dgx-spark-dspark-mia-${UID}}"
default_lock_dir="${HOME}/.local/state/dgx-spark-dspark-mia-locks"
lock_dir="${MIA_SUPERVISOR_LOCK_DIR:-${MIA_SUPERVISOR_RUNTIME_DIR:-${default_lock_dir}}}"
poll_seconds="${MIA_SUPERVISOR_POLL_SECONDS:-10}"
ssh_failure_threshold="${MIA_SUPERVISOR_SSH_FAILURE_THRESHOLD:-3}"
degraded_ssh_threshold="${MIA_SUPERVISOR_DEGRADED_SSH_FAILURE_THRESHOLD:-12}"
api_failure_threshold="${MIA_SUPERVISOR_API_FAILURE_THRESHOLD:-3}"
backoff_initial="${MIA_SUPERVISOR_BACKOFF_INITIAL_SECONDS:-15}"
backoff_max="${MIA_SUPERVISOR_BACKOFF_MAX_SECONDS:-300}"
stable_checks_for_reset="${MIA_SUPERVISOR_STABLE_CHECKS_FOR_RESET:-6}"
probe_timeout="${MIA_SUPERVISOR_PROBE_TIMEOUT_SECONDS:-60}"
stop_timeout="${MIA_SUPERVISOR_STOP_TIMEOUT_SECONDS:-180}"
start_timeout="${MIA_SUPERVISOR_START_TIMEOUT_SECONDS:-2100}"
max_checks="${MIA_SUPERVISOR_MAX_CHECKS:-0}"

for value_name in poll_seconds ssh_failure_threshold degraded_ssh_threshold \
  api_failure_threshold backoff_initial backoff_max stable_checks_for_reset \
  probe_timeout stop_timeout start_timeout; do
  value="${!value_name}"
  if [[ ! "${value}" =~ ^[1-9][0-9]*$ ]]; then
    echo "${value_name} must be a positive integer." >&2
    exit 2
  fi
done
if [[ ! "${max_checks}" =~ ^[0-9]+$ ]]; then
  echo "max_checks must be a non-negative integer." >&2
  exit 2
fi
if ((backoff_initial > backoff_max)); then
  echo "Initial recovery backoff cannot exceed its maximum." >&2
  exit 2
fi

umask 0077
if [[ -L "${state_dir}" || -L "${runtime_dir}" || -L "${lock_dir}" ]]; then
  echo "Supervisor state/runtime/lock directories cannot be symlinks." >&2
  exit 2
fi
mkdir -p -- "${state_dir}" "${runtime_dir}" "${lock_dir}"
chmod 0700 -- "${state_dir}" "${runtime_dir}" "${lock_dir}"

epoch_file="${state_dir}/epoch"
owner_file="${state_dir}/owner-active"
stop_file="${runtime_dir}/stop-requested"
lock_file="${lock_dir}/supervisor.lock"
recovery_lock="${lock_dir}/recovery.lock"

exec {supervisor_lock_fd}>"${lock_file}"
if ! flock -n "${supervisor_lock_fd}"; then
  echo "Another DSpark supervisor already owns ${lock_file}." >&2
  exit 75
fi
exec {recovery_lock_fd}>"${recovery_lock}"
rm -f -- "${stop_file}"
printf '%s\n' "$$" >"${owner_file}"

shutdown_requested=0
active_child=0

log() {
  printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"
}

request_shutdown() {
  shutdown_requested=1
  : >"${stop_file}"
  if ((active_child > 0)); then
    kill -TERM -- "-${active_child}" 2>/dev/null ||
      kill -TERM "${active_child}" 2>/dev/null ||
      true
  fi
}
trap request_shutdown TERM INT HUP

should_stop() {
  ((shutdown_requested)) || [[ -e "${stop_file}" ]]
}

run_child() {
  setsid --wait "$@" &
  active_child=$!
  set +e
  wait "${active_child}"
  child_status=$?
  set -e
  active_child=0
  return "${child_status}"
}

sleep_interruptibly() {
  local duration="$1"
  if should_stop; then
    return 1
  fi
  setsid --wait sleep "${duration}" &
  active_child=$!
  set +e
  wait "${active_child}"
  sleep_status=$?
  set -e
  active_child=0
  if should_stop; then
    return 1
  fi
  return "${sleep_status}"
}

probe() {
  set +e
  probe_output="$(
    timeout --kill-after=5 "${probe_timeout}" \
      "${helper_dir}/probe.sh" 2>&1
  )"
  probe_status=$?
  set -e
  if ((probe_status == 124 || probe_status == 137)); then
    probe_status=15
    probe_output="health probe exceeded its ${probe_timeout}-second wall-clock limit"
  fi
  return "${probe_status}"
}

save_epoch() {
  local fingerprint="$1"
  local temporary="${epoch_file}.new.$$"
  printf '%s\n' "${fingerprint}" >"${temporary}"
  chmod 0600 "${temporary}"
  mv -f -- "${temporary}" "${epoch_file}"
}

probe_fingerprint() {
  sed -n 's/^FINGERPRINT=//p' <<<"${probe_output}" | tail -n 1
}

failure_threshold() {
  case "$1" in
    10|12|14) printf '1\n' ;;
    11) printf '%s\n' "${ssh_failure_threshold}" ;;
    16) printf '%s\n' "${degraded_ssh_threshold}" ;;
    13) printf '%s\n' "${api_failure_threshold}" ;;
    *) printf '2\n' ;;
  esac
}

coordinated_stop() {
  log "Stopping the scoped TP2 generation on both nodes."
  set +e
  run_child env MIA_SUPERVISOR_CHILD=1 \
    timeout --kill-after=15 "${stop_timeout}" \
      "${helper_dir}/stop.sh"
  stop_status=$?
  set -e
  if ((stop_status != 0)); then
    log "Scoped TP2 cleanup returned status ${stop_status}; startup preflight will recheck both nodes."
  fi
  return "${stop_status}"
}

coordinated_start() {
  log "Starting a fresh worker-first TP2 generation."
  run_child env MIA_SUPERVISOR_CHILD=1 \
    timeout --kill-after=30 "${start_timeout}" \
      "${helper_dir}/start.sh"
}

lock_recovery() {
  set +e
  flock -x "${recovery_lock_fd}"
  lock_status=$?
  set -e
  return "${lock_status}"
}

unlock_recovery() {
  flock -u "${recovery_lock_fd}" || true
}

recover_until_healthy() {
  local reason="$1"
  local backoff="$2"
  local fingerprint=""
  local post_start_healthy=0
  log "Recovery required: ${reason}"
  while ! should_stop; do
    if ! lock_recovery; then
      should_stop && return 1
      log "Could not acquire the coordinated-recovery lock."
      recovery_status=1
    elif should_stop; then
      unlock_recovery
      return 1
    else
      coordinated_stop || true
      if should_stop; then
        unlock_recovery
        return 1
      fi
      set +e
      coordinated_start
      recovery_status=$?
      set -e

      if ((recovery_status == 0)); then
        if probe; then
          fingerprint="$(probe_fingerprint)"
          if [[ -n "${fingerprint}" ]]; then
            save_epoch "${fingerprint}"
            printf '%s\n' "$$" >"${owner_file}"
            post_start_healthy=1
          else
            probe_output="post-start probe omitted its generation fingerprint"
          fi
        fi
        if ((post_start_healthy == 0)); then
          log "Startup returned success but the post-start identity probe failed: ${probe_output}"
        fi
      fi

      unlock_recovery
    fi

    if ((post_start_healthy)); then
      log "Recovery complete; both ranks and ${SERVED_MODEL_NAME} are healthy."
      RECOVERY_BACKOFF="${backoff}"
      return 0
    elif should_stop; then
      return 1
    elif ((recovery_status != 0)); then
      log "Recovery attempt failed with status ${recovery_status}."
    fi

    log "Retrying coordinated recovery in ${backoff} seconds."
    sleep_interruptibly "${backoff}" || return 1
    if ((backoff < backoff_max)); then
      backoff=$((backoff * 2))
      ((backoff > backoff_max)) && backoff="${backoff_max}"
    fi
    RECOVERY_BACKOFF="${backoff}"
  done
  return 1
}

log "Spark 1 DSpark supervisor started for project ${MIA_PROJECT_NAME}."
failure_count=0
healthy_checks=0
check_count=0
RECOVERY_BACKOFF="${backoff_initial}"

while ! should_stop; do
  check_count=$((check_count + 1))
  if probe; then
    fingerprint="$(probe_fingerprint)"
    expected=""
    [[ -r "${epoch_file}" ]] && expected="$(<"${epoch_file}")"
    if [[ -z "${fingerprint}" ]]; then
      probe_status=14
      probe_output="healthy probe omitted its generation fingerprint"
    elif [[ -n "${expected}" && "${fingerprint}" != "${expected}" ]]; then
      probe_status=14
      probe_output="rank generation identity changed outside coordinated recovery"
    else
      if [[ -z "${expected}" ]]; then
        save_epoch "${fingerprint}"
        log "Adopted the existing healthy TP2 generation."
      elif ((healthy_checks == 0)); then
        log "Both ranks and the model API are healthy."
      fi
      failure_count=0
      healthy_checks=$((healthy_checks + 1))
      if ((healthy_checks >= stable_checks_for_reset)); then
        RECOVERY_BACKOFF="${backoff_initial}"
      fi
      if ((max_checks > 0 && check_count >= max_checks)); then
        break
      fi
      sleep_interruptibly "${poll_seconds}" || break
      continue
    fi
  fi

  healthy_checks=0
  failure_count=$((failure_count + 1))
  threshold="$(failure_threshold "${probe_status}")"
  log "Health failure ${failure_count}/${threshold} (status ${probe_status}): ${probe_output}"
  if ((failure_count >= threshold)); then
    recover_until_healthy "${probe_output}" "${RECOVERY_BACKOFF}" || true
    failure_count=0
    healthy_checks=0
  fi
  if ((max_checks > 0 && check_count >= max_checks)); then
    break
  fi
  sleep_interruptibly "${poll_seconds}" || break
done

if should_stop; then
  log "Supervisor shutdown requested; cleaning both scoped ranks."
  if lock_recovery; then
    coordinated_stop || true
    unlock_recovery
  else
    log "Could not acquire the recovery lock during shutdown; ExecStopPost will retry cleanup."
  fi
fi
log "Spark 1 DSpark supervisor stopped."
