#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ssh
need_command timeout
require_head_host
require_ssh_identity
acquire_lifecycle_lock

stop_timeout="${MIA3_STOP_COMMAND_TIMEOUT_SECONDS:-90}"
[[ "${stop_timeout}" =~ ^[1-9][0-9]*$ ]] || { echo "Stop timeout must be positive." >&2; exit 2; }

echo "Stopping API rank 0, then worker ranks 1 and 2 for project ${MIA_PROJECT_NAME}..."
statuses=()
set +e
timeout --kill-after=10 "${stop_timeout}" \
  "${MIA3_ROOT}/bin/node-compose.sh" 0 down --timeout 30
statuses+=("$?")
timeout --kill-after=10 "${stop_timeout}" \
  bash -c 'source "$1/bin/common.sh"; remote_trial_command 1 node-compose.sh 1 down --timeout 30' \
  -- "${MIA3_ROOT}"
statuses+=("$?")
timeout --kill-after=10 "${stop_timeout}" \
  bash -c 'source "$1/bin/common.sh"; remote_trial_command 2 node-compose.sh 2 down --timeout 30' \
  -- "${MIA3_ROOT}"
statuses+=("$?")
set -e

failed=0
for status in "${statuses[@]}"; do
  ((status == 0)) || failed=1
done
echo "Trial cleanup complete; production projects were not addressed."
exit "${failed}"
