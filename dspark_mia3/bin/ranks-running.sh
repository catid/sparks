#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ssh
require_ssh_identity

rank_running() {
  local rank="$1" count
  if [[ "${rank}" == 0 ]]; then
    count="$("${MIA3_ROOT}/bin/node-compose.sh" 0 ps --status running -q | sed '/^$/d' | wc -l)"
  else
    count="$(remote_trial_command "${rank}" node-compose.sh "${rank}" ps --status running -q | sed '/^$/d' | wc -l)"
  fi
  [[ "${count}" == 1 ]] || {
    echo "$(rank_host "${rank}") rank ${rank}: expected one running trial container, found ${count}." >&2
    return 1
  }
}

# Dependency order makes logs easier to read when a worker disappears.
rank_running 2
rank_running 1
rank_running 0
