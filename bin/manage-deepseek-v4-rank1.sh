#!/usr/bin/env bash
set -euo pipefail

# Coordinate Spark 2's headless vLLM rank from Spark 1. All remote command
# strings are constants; configurable values are passed only as local ssh
# arguments, preventing environment values from becoming remote shell syntax.

readonly rank1_unit="dgx-spark-deepseek-v4-rank1.service"
action="${1:-}"
rank1_host="${DEEPSEEK_RANK1_HOST:-192.168.100.11}"
rank1_user="${DEEPSEEK_RANK1_SSH_USER:-catid}"
ssh_key="${DEEPSEEK_RANK1_SSH_KEY:-/home/catid/.ssh/id_ed25519_dgx_cluster}"
known_hosts="${DEEPSEEK_RANK1_KNOWN_HOSTS:-/home/catid/.ssh/known_hosts}"
control_protocol="${DEEPSEEK_RANK1_CONTROL_PROTOCOL:-legacy-shell-v1}"
wait_timeout="${DEEPSEEK_RANK1_WAIT_TIMEOUT_SECONDS:-300}"
stop_timeout="${DEEPSEEK_RANK1_STOP_TIMEOUT_SECONDS:-330}"
poll_seconds="${DEEPSEEK_RANK1_POLL_SECONDS:-2}"
stable_seconds="${DEEPSEEK_RANK1_STABLE_SECONDS:-5}"
readonly forced_status_request="DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STATUS"
readonly forced_restart_request="DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_RESTART"
readonly forced_stop_request="DGX_SPARK_DEEPSEEK_V4_RANK1_CONTROL_V1_STOP"

usage() {
  cat <<'EOF'
Usage: manage-deepseek-v4-rank1.sh ACTION

Actions:
  start-wait  Restart rank 1 and wait for a new, stable systemd invocation.
  stop-wait   Stop rank 1 and wait until its complete cgroup is gone.
  status      Print rank 1's systemd state.
  describe    Print the controller configuration without connecting.

The remote unit name and every remote command are deliberately fixed. Host,
user, key, known-hosts path, and timeout settings may be supplied through the
DEEPSEEK_RANK1_* variables in /etc/dgx-spark-deepseek-v4.env.

DEEPSEEK_RANK1_CONTROL_PROTOCOL selects either forced-command-v1 (production)
or legacy-shell-v1 (migration only). The legacy default preserves existing
deployments until their dedicated forced-command key is explicitly installed.
EOF
}

require_uint() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s must be an unsigned integer (got %q)\n' "${name}" "${value}" >&2
    exit 2
  fi
}

case "${action}" in
  start-wait | stop-wait | status | describe) ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

if [[ ! "${rank1_host}" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
  printf 'Unsafe DEEPSEEK_RANK1_HOST value: %q\n' "${rank1_host}" >&2
  exit 2
fi
if [[ ! "${rank1_user}" =~ ^[a-z_][a-z0-9_-]*\$?$ ]]; then
  printf 'Unsafe DEEPSEEK_RANK1_SSH_USER value: %q\n' "${rank1_user}" >&2
  exit 2
fi
if [[ "${ssh_key}" != /* || "${known_hosts}" != /* ]]; then
  echo "SSH key and known-hosts paths must be absolute." >&2
  exit 2
fi
case "${control_protocol}" in
  forced-command-v1 | legacy-shell-v1) ;;
  *)
    printf 'Unsupported DEEPSEEK_RANK1_CONTROL_PROTOCOL: %q\n' \
      "${control_protocol}" >&2
    exit 2
    ;;
esac

require_uint DEEPSEEK_RANK1_WAIT_TIMEOUT_SECONDS "${wait_timeout}"
require_uint DEEPSEEK_RANK1_STOP_TIMEOUT_SECONDS "${stop_timeout}"
require_uint DEEPSEEK_RANK1_POLL_SECONDS "${poll_seconds}"
require_uint DEEPSEEK_RANK1_STABLE_SECONDS "${stable_seconds}"
if ((wait_timeout < 1 || stop_timeout < 1 || poll_seconds < 1 || stable_seconds < 1)); then
  echo "Rank-1 controller timeouts and intervals must be positive." >&2
  exit 2
fi

destination="${rank1_user}@${rank1_host}"
readonly -a ssh_options=(
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o IdentityAgent=none
  -o ConnectTimeout=5
  -o ConnectionAttempts=1
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=20
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=${known_hosts}"
  -i "${ssh_key}"
)

if [[ "${action}" == "describe" ]]; then
  printf 'destination=%s\nunit=%s\nprotocol=%s\nkey=%s\nknown_hosts=%s\n' \
    "${destination}" "${rank1_unit}" "${control_protocol}" \
    "${ssh_key}" "${known_hosts}"
  printf 'wait_timeout=%s stop_timeout=%s poll=%s stable=%s\n' \
    "${wait_timeout}" "${stop_timeout}" "${poll_seconds}" "${stable_seconds}"
  exit 0
fi

if [[ ! -r "${ssh_key}" ]]; then
  printf 'Rank-1 SSH key is not readable: %s\n' "${ssh_key}" >&2
  exit 2
fi
if [[ ! -r "${known_hosts}" ]]; then
  printf 'SSH known-hosts file is not readable: %s\n' "${known_hosts}" >&2
  exit 2
fi

show_remote_state() {
  if [[ "${control_protocol}" == "forced-command-v1" ]]; then
    # The locally expanded value is a readonly constant, never user input.
    # shellcheck disable=SC2029
    LC_ALL=C /usr/bin/ssh "${ssh_options[@]}" "${destination}" \
      "${forced_status_request}"
  else
    LC_ALL=C /usr/bin/ssh "${ssh_options[@]}" "${destination}" \
      '/usr/bin/systemctl show dgx-spark-deepseek-v4-rank1.service -p LoadState -p ActiveState -p SubState -p MainPID -p InvocationID'
  fi
}

restart_remote_rank() {
  if [[ "${control_protocol}" == "forced-command-v1" ]]; then
    # The locally expanded value is a readonly constant, never user input.
    # shellcheck disable=SC2029
    LC_ALL=C /usr/bin/ssh "${ssh_options[@]}" "${destination}" \
      "${forced_restart_request}"
  else
    LC_ALL=C /usr/bin/ssh "${ssh_options[@]}" "${destination}" \
      '/usr/bin/sudo -n /usr/bin/systemctl reset-failed dgx-spark-deepseek-v4-rank1.service >/dev/null 2>&1 || true; exec /usr/bin/sudo -n /usr/bin/systemctl restart --no-block dgx-spark-deepseek-v4-rank1.service'
  fi
}

stop_remote_rank() {
  if [[ "${control_protocol}" == "forced-command-v1" ]]; then
    # The locally expanded value is a readonly constant, never user input.
    # shellcheck disable=SC2029
    LC_ALL=C /usr/bin/ssh "${ssh_options[@]}" "${destination}" \
      "${forced_stop_request}"
  else
    LC_ALL=C /usr/bin/ssh "${ssh_options[@]}" "${destination}" \
      'exec /usr/bin/sudo -n /usr/bin/systemctl stop --no-block dgx-spark-deepseek-v4-rank1.service'
  fi
}

state_load=""
state_active=""
state_sub=""
state_pid="0"
state_invocation=""

query_state() {
  local output key value
  state_load=""
  state_active=""
  state_sub=""
  state_pid="0"
  state_invocation=""

  if ! output="$(show_remote_state)"; then
    return 1
  fi
  while IFS='=' read -r key value; do
    case "${key}" in
      LoadState) state_load="${value}" ;;
      ActiveState) state_active="${value}" ;;
      SubState) state_sub="${value}" ;;
      MainPID) state_pid="${value}" ;;
      InvocationID) state_invocation="${value}" ;;
    esac
  done <<<"${output}"
  [[ "${state_pid}" =~ ^[0-9]+$ ]]
}

print_state() {
  printf 'unit=%s load=%s active=%s sub=%s pid=%s invocation=%s\n' \
    "${rank1_unit}" "${state_load:-unknown}" "${state_active:-unknown}" \
    "${state_sub:-unknown}" "${state_pid:-0}" "${state_invocation:-none}"
}

if [[ "${action}" == "status" ]]; then
  query_state
  print_state
  exit 0
fi

if [[ "${action}" == "start-wait" ]]; then
  old_invocation=""
  if query_state; then
    old_invocation="${state_invocation}"
  fi

  start_seconds="${SECONDS}"
  echo "Requesting a fresh ${rank1_unit} invocation on ${destination}."
  while ! restart_remote_rank; do
    if ((SECONDS - start_seconds >= wait_timeout)); then
      printf 'Timed out after %d seconds requesting remote rank-1 restart.\n' \
        "${wait_timeout}" >&2
      exit 1
    fi
    echo "Spark 2 SSH/systemd is not ready; retrying." >&2
    /usr/bin/sleep "${poll_seconds}"
  done

  while true; do
    if query_state; then
      if [[ "${state_load}" == "not-found" ]]; then
        echo "Remote rank-1 unit is not installed." >&2
        exit 1
      fi
      if [[ "${state_active}" == "failed" &&
        "${state_invocation}" != "${old_invocation}" ]]; then
        echo "Fresh rank-1 invocation failed during startup." >&2
        print_state >&2
        exit 1
      fi

      if [[ "${state_active}" == "active" && "${state_sub}" == "running" &&
        "${state_pid}" != "0" && -n "${state_invocation}" &&
        "${state_invocation}" != "${old_invocation}" ]]; then
        candidate_pid="${state_pid}"
        candidate_invocation="${state_invocation}"
        /usr/bin/sleep "${stable_seconds}"
        if query_state &&
          [[ "${state_active}" == "active" && "${state_sub}" == "running" &&
            "${state_pid}" == "${candidate_pid}" &&
            "${state_invocation}" == "${candidate_invocation}" ]]; then
          echo "Remote rank 1 is running with a stable, fresh invocation."
          print_state
          exit 0
        fi
      fi
    fi

    if ((SECONDS - start_seconds >= wait_timeout)); then
      printf 'Timed out after %d seconds waiting for remote rank 1.\n' \
        "${wait_timeout}" >&2
      if query_state; then
        print_state >&2
      fi
      exit 1
    fi
    /usr/bin/sleep "${poll_seconds}"
  done
fi

echo "Requesting ${rank1_unit} stop on ${destination}."
start_seconds="${SECONDS}"
while ! stop_remote_rank; do
  if ((SECONDS - start_seconds >= stop_timeout)); then
    printf 'Timed out after %d seconds requesting remote rank-1 stop.\n' \
      "${stop_timeout}" >&2
    exit 1
  fi
  echo "Spark 2 SSH/systemd is not ready for stop; retrying." >&2
  /usr/bin/sleep "${poll_seconds}"
done

while true; do
  if query_state; then
    if [[ ("${state_active}" == "inactive" || "${state_active}" == "failed") &&
      "${state_pid}" == "0" ]]; then
      echo "Remote rank 1 is stopped."
      print_state
      exit 0
    fi
  fi

  if ((SECONDS - start_seconds >= stop_timeout)); then
    printf 'Timed out after %d seconds waiting for remote rank 1 to stop.\n' \
      "${stop_timeout}" >&2
    if query_state; then
      print_state >&2
    fi
    exit 1
  fi
  /usr/bin/sleep "${poll_seconds}"
done
