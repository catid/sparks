#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

rank="${1:-0}"
lines="${2:-200}"
[[ "${rank}" =~ ^[012]$ && "${lines}" =~ ^[1-9][0-9]*$ ]] || { echo "Usage: $0 {0|1|2} [lines]" >&2; exit 2; }
if [[ "${rank}" == 0 ]]; then
  exec "${MIA3_ROOT}/bin/node-compose.sh" 0 logs --tail "${lines}" vllm-dspark
fi
remote_trial_command "${rank}" node-compose.sh "${rank}" logs --tail "${lines}" vllm-dspark
