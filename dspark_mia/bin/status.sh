#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

helper_dir="${MIA_STATUS_HELPER_DIR:-${MIA_ROOT}/bin}"
load_nonlaunch_compose_addresses 0
remote_nonlaunch_env="$(remote_nonlaunch_assignment)"

echo "== cerberus1 / rank 0 =="
set +e
"${helper_dir}/node-compose.sh" 0 ps -a
local_status=$?
set -e

echo "== cerberus2 / rank 1 =="
if [[ ! -f "${CLUSTER_SSH_KEY}" ]]; then
  echo "Worker status unavailable: missing cluster SSH identity ${CLUSTER_SSH_KEY}." >&2
  remote_status=2
elif ! command -v ssh >/dev/null 2>&1; then
  echo "Worker status unavailable: ssh is not installed." >&2
  remote_status=2
else
  set +e
  ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
    "env ${remote_nonlaunch_env} '${WORKER_INSTALL_DIR}/bin/node-compose.sh' 1 ps -a"
  remote_status=$?
  set -e
fi

if ((local_status != 0)); then
  echo "Head rank status failed with status ${local_status}." >&2
fi
if ((remote_status != 0)); then
  echo "Worker rank status is unavailable (status ${remote_status}); head status above is still authoritative." >&2
fi
if ((local_status != 0 || remote_status != 0)); then
  exit 1
fi
