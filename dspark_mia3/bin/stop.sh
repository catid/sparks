#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ssh
need_command timeout
require_head_host
acquire_lifecycle_lock

stop_timeout="${MIA3_STOP_COMMAND_TIMEOUT_SECONDS:-90}"
[[ "${stop_timeout}" =~ ^[1-9][0-9]*$ ]] || { echo "Stop timeout must be positive." >&2; exit 2; }

echo "Stopping only trial project ${MIA_PROJECT_NAME}; local cleanup starts independently..."
timeout --kill-after=10 "${stop_timeout}" \
  "${MIA3_ROOT}/bin/node-compose.sh" 0 down --timeout 30 &
local_pid=$!

worker_pids=()
worker_launch_failed=0
if [[ ! -f "${CLUSTER_SSH_KEY}" ]]; then
  echo "Worker cleanup unavailable: missing cluster SSH identity ${CLUSTER_SSH_KEY}." >&2
  worker_launch_failed=1
else
  for rank in 1 2; do
    timeout --kill-after=10 "${stop_timeout}" \
      bash -c 'source "$1/bin/common.sh"; remote_trial_command "$2" node-compose.sh "$2" down --timeout 30' \
      -- "${MIA3_ROOT}" "${rank}" &
    worker_pids+=("$!")
  done
fi

set +e
wait "${local_pid}"
local_status=$?
worker_status="${worker_launch_failed}"
for pid in "${worker_pids[@]}"; do
  wait "${pid}"
  status=$?
  ((status == 0)) || worker_status=1
done
set -e

((local_status == 0)) || echo "Head cleanup returned status ${local_status}." >&2
((worker_status == 0)) || echo "Worker cleanup is incomplete; local cleanup was still attempted." >&2
echo "Trial cleanup complete; production projects were not addressed."
((local_status == 0 && worker_status == 0))
