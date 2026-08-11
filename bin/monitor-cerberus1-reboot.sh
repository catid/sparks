#!/usr/bin/env bash
#
# Observe Cerberus 1 from Cerberus 2 while Cerberus 1 reboots. This script only
# sends ICMP echo requests and HTTP GET requests, and appends results to the log
# path supplied by the caller.
#
# Usage:
#   monitor-cerberus1-reboot.sh LOG_FILE [INTERVAL_SECONDS] [MAX_SECONDS]
#
# Defaults: one snapshot every 10 seconds, for at most 1800 seconds (30 min).

set -euo pipefail

usage() {
  echo "Usage: $0 LOG_FILE [INTERVAL_SECONDS] [MAX_SECONDS]" >&2
}

if (( $# < 1 || $# > 3 )); then
  usage
  exit 2
fi

log_file="$1"
interval_seconds="${2:-10}"
max_seconds="${3:-1800}"
monitor_hostname="$(hostname -s)"

if [[ -z "${log_file}" ]]; then
  echo "LOG_FILE must not be empty." >&2
  exit 2
fi

if [[ ! "${interval_seconds}" =~ ^[0-9]+$ ]] ||
   (( interval_seconds < 1 || interval_seconds > 60 )); then
  echo "INTERVAL_SECONDS must be an integer from 1 through 60." >&2
  exit 2
fi

if [[ ! "${max_seconds}" =~ ^[0-9]+$ ]] ||
   (( max_seconds < 1 || max_seconds > 1800 )); then
  echo "MAX_SECONDS must be an integer from 1 through 1800." >&2
  exit 2
fi

case "${monitor_hostname}" in
  cerberus2 | cerebrus2 | spark2) monitor_role=cerberus2 ;;
  *) monitor_role="" ;;
esac
allow_non_cerberus2="${ALLOW_NON_CERBERUS2_MONITOR:-${ALLOW_NON_SPARK2_MONITOR:-0}}"
if [[ "${monitor_role}" != "cerberus2" &&
      "${allow_non_cerberus2}" != "1" ]]; then
  echo "Run this monitor on cerberus2 so it survives the cerberus1 reboot." >&2
  echo "Set ALLOW_NON_CERBERUS2_MONITOR=1 only for deliberate testing." >&2
  exit 2
fi

log_dir="$(dirname -- "${log_file}")"
if [[ ! -d "${log_dir}" ]]; then
  echo "Log directory does not exist: ${log_dir}" >&2
  exit 2
fi
if [[ -e "${log_file}" && ! -f "${log_file}" ]]; then
  echo "Log path is not a regular file: ${log_file}" >&2
  exit 2
fi
if [[ ! -w "${log_dir}" ]]; then
  echo "Log directory is not writable: ${log_dir}" >&2
  exit 2
fi

# Canonical mDNS follows the host across DHCP lease changes. The SPARK1_*
# fallbacks are input compatibility only and are never emitted as defaults.
cerberus1_host="${CERBERUS1_HOST:-${SPARK1_LAN_HOST:-cerberus1.local}}"
cerberus1_cx7_host="${CERBERUS1_CX7_HOST:-${SPARK1_CX7_HOST:-192.168.0.1}}"
curl_connect_timeout="${MONITOR_CONNECT_TIMEOUT_SECONDS:-1}"
curl_max_time="${MONITOR_MAX_TIME_SECONDS:-1.5}"

timestamp() {
  date --iso-8601=seconds
}

log_line() {
  printf '%s\n' "$*" | tee -a "${log_file}"
}

probe_ping() {
  local snapshot_timestamp="$1"
  local cycle="$2"
  local label="$3"
  local target="$4"
  local rc
  local reachable=false

  set +e
  ping -n -c 1 -W 1 "${target}" >/dev/null 2>&1
  rc=$?
  set -e
  if (( rc == 0 )); then
    reachable=true
  fi

  log_line \
    "${snapshot_timestamp} cycle=${cycle} check=${label}" \
    "target=${target} reachable=${reachable} ping_exit=${rc}"
}

probe_http() {
  local snapshot_timestamp="$1"
  local cycle="$2"
  local label="$3"
  local url="$4"
  local tls_insecure="${5:-false}"
  local curl_args=(
    --silent
    --output /dev/null
    --connect-timeout "${curl_connect_timeout}"
    --max-time "${curl_max_time}"
    --write-out
    "http_code=%{http_code} remote_ip=%{remote_ip} connect_s=%{time_connect} total_s=%{time_total}"
  )
  local result
  local rc
  local reachable=false

  if [[ "${tls_insecure}" == "true" ]]; then
    curl_args+=(--insecure)
  fi

  set +e
  result="$(curl "${curl_args[@]}" "${url}")"
  rc=$?
  set -e
  if (( rc == 0 )); then
    reachable=true
  fi

  log_line \
    "${snapshot_timestamp} cycle=${cycle} check=${label}" \
    "target=${url} reachable=${reachable} curl_exit=${rc} ${result}"
}

# Opening in append mode also verifies the requested log is writable. Existing
# reboot history is deliberately preserved.
: >>"${log_file}"

started_at="$(timestamp)"
started_seconds="${SECONDS}"
deadline_seconds=$((started_seconds + max_seconds))
cycle=0

log_line \
  "${started_at} event=monitor_start observer=${monitor_hostname}" \
  "observer_role=${monitor_role:-non-cluster}" \
  "interval_s=${interval_seconds} max_s=${max_seconds}" \
  "cerberus1_host=${cerberus1_host} cerberus1_cx7=${cerberus1_cx7_host}"

while (( SECONDS < deadline_seconds )); do
  cycle=$((cycle + 1))
  cycle_started="${SECONDS}"
  snapshot_timestamp="$(timestamp)"

  probe_ping \
    "${snapshot_timestamp}" "${cycle}" "ping_lan" "${cerberus1_host}"
  probe_ping \
    "${snapshot_timestamp}" "${cycle}" "ping_cx7" "${cerberus1_cx7_host}"
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "dashboard_http_80" \
    "http://${cerberus1_host}/"
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "dashboard_https_443" \
    "https://${cerberus1_host}/" true
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "dashboard_http_8090" \
    "http://${cerberus1_host}:8090/"
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "dashboard_status_8090" \
    "http://${cerberus1_host}:8090/api/status"
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "api_models_8080" \
    "http://${cerberus1_host}:8080/v1/models"
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "router_readiness_8080" \
    "http://${cerberus1_host}:8080/readiness"
  probe_http \
    "${snapshot_timestamp}" "${cycle}" "cerberus1_backend_health" \
    "http://${cerberus1_cx7_host}:8000/health"

  remaining_seconds=$((deadline_seconds - SECONDS))
  if (( remaining_seconds <= 0 )); then
    break
  fi

  cycle_elapsed=$((SECONDS - cycle_started))
  sleep_seconds=$((interval_seconds - cycle_elapsed))
  if (( sleep_seconds < 1 )); then
    sleep_seconds=1
  fi
  if (( sleep_seconds > remaining_seconds )); then
    sleep_seconds="${remaining_seconds}"
  fi
  sleep "${sleep_seconds}"
done

log_line \
  "$(timestamp) event=monitor_complete observer=${monitor_hostname}" \
  "cycles=${cycle} elapsed_s=$((SECONDS - started_seconds))"
