#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

state_dir="${MIA_SUPERVISOR_STATE_DIR:-${XDG_STATE_HOME:-/tmp}/dgx-spark-dspark-mia-${UID}}"
owner_file="${state_dir}/owner-active"
epoch_file="${state_dir}/epoch"

if [[ ! -f "${owner_file}" ]]; then
  echo "No supervisor ownership marker; leaving existing containers unchanged."
  exit 0
fi

"${MIA_ROOT}/bin/stop.sh"
rm -f -- "${owner_file}" "${epoch_file}"
echo "Supervisor-owned DSpark ranks are stopped."
