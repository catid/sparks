#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command timeout
acquire_lifecycle_locks
helper_dir="${MIA_STOP_HELPER_DIR:-${MIA_ROOT}/bin}"
load_nonlaunch_compose_addresses 0
remote_nonlaunch_env="$(remote_nonlaunch_assignment)"
stop_timeout="${MIA_STOP_COMMAND_TIMEOUT_SECONDS:-90}"
if [[ ! "${stop_timeout}" =~ ^[1-9][0-9]*$ ]]; then
  echo "MIA_STOP_COMMAND_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi

echo "Stopping only pinned TP2 project ${MIA_PROJECT_NAME} on both nodes..."
timeout --kill-after=10 "${stop_timeout}" \
  "${helper_dir}/node-compose.sh" 0 down --timeout 30 &
head_stop_pid=$!

worker_stop_pid=""
worker_stop_status=0
if [[ ! -f "${CLUSTER_SSH_KEY}" ]]; then
  echo "Worker cleanup unavailable: missing cluster SSH identity ${CLUSTER_SSH_KEY}." >&2
  worker_stop_status=2
elif ! command -v ssh >/dev/null 2>&1; then
  echo "Worker cleanup unavailable: ssh is not installed." >&2
  worker_stop_status=2
else
  timeout --kill-after=10 "${stop_timeout}" \
    ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
      "if [[ -x '${WORKER_INSTALL_DIR}/bin/node-compose.sh' ]]; then timeout --kill-after=10 '${stop_timeout}' env ${remote_nonlaunch_env} '${WORKER_INSTALL_DIR}/bin/node-compose.sh' 1 down --timeout 30; else echo 'Worker node-compose helper is missing.' >&2; exit 1; fi" &
  worker_stop_pid=$!
fi

set +e
wait "${head_stop_pid}"
head_stop_status=$?
if [[ -n "${worker_stop_pid}" ]]; then
  wait "${worker_stop_pid}"
  worker_stop_status=$?
fi
set -e
if ((head_stop_status != 0)); then
  echo "Head cleanup returned status ${head_stop_status}; continuing best-effort recovery." >&2
fi
if ((worker_stop_status != 0)); then
  echo "Worker cleanup returned status ${worker_stop_status}; head cleanup was attempted independently." >&2
fi

if ((head_stop_status == 0 && worker_stop_status == 0)); then
  echo "Pinned Mia DSpark project is down on both nodes. Existing port-8000 units were not changed."
elif ((head_stop_status == 0)); then
  echo "Pinned head project is down; worker cleanup is unconfirmed. Existing port-8000 units were not changed." >&2
fi
if ((head_stop_status != 0 || worker_stop_status != 0)); then
  exit 1
fi
