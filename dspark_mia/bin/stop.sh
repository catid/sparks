#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ssh
need_command timeout
require_ssh_identity
acquire_lifecycle_locks
remote_profile_env="$(remote_profile_assignment)"
stop_timeout="${MIA_STOP_COMMAND_TIMEOUT_SECONDS:-90}"
if [[ ! "${stop_timeout}" =~ ^[1-9][0-9]*$ ]]; then
  echo "MIA_STOP_COMMAND_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi

echo "Stopping only pinned TP2 project ${MIA_PROJECT_NAME} on both nodes..."
timeout --kill-after=10 "${stop_timeout}" \
  "${MIA_ROOT}/bin/node-compose.sh" 0 down --timeout 30 &
head_stop_pid=$!
timeout --kill-after=10 "${stop_timeout}" \
  ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
    "if [[ -x '${WORKER_INSTALL_DIR}/bin/node-compose.sh' ]]; then timeout --kill-after=10 '${stop_timeout}' env ${remote_profile_env} '${WORKER_INSTALL_DIR}/bin/node-compose.sh' 1 down --timeout 30; fi" &
worker_stop_pid=$!

set +e
wait "${head_stop_pid}"
head_stop_status=$?
wait "${worker_stop_pid}"
worker_stop_status=$?
set -e
if ((head_stop_status != 0)); then
  echo "Head cleanup returned status ${head_stop_status}; continuing best-effort recovery." >&2
fi
if ((worker_stop_status != 0)); then
  echo "Worker cleanup returned status ${worker_stop_status}; continuing best-effort recovery." >&2
fi

echo "Pinned Mia DSpark project is down. Existing port-8000 units were not changed."
if ((head_stop_status != 0 || worker_stop_status != 0)); then
  exit 1
fi
