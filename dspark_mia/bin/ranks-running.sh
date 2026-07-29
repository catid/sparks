#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote command uses validated profile values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command docker
need_command ssh
need_command sudo
need_command timeout
require_ssh_identity

if [[ "$(/usr/bin/hostname -s)" != "spark1" ]]; then
  echo "Run the two-node rank check from spark1." >&2
  exit 2
fi

rank_timeout="${MIA_RANK_CHECK_TIMEOUT_SECONDS:-15}"
if [[ ! "${rank_timeout}" =~ ^[1-9][0-9]*$ ]]; then
  echo "MIA_RANK_CHECK_TIMEOUT_SECONDS must be a positive integer." >&2
  exit 2
fi

set +e
local_id_output="$(
  timeout --kill-after=2 "${rank_timeout}" \
    sudo -n docker ps -q \
      --filter "label=com.docker.compose.project=${MIA_PROJECT_NAME}" \
      --filter "label=com.docker.compose.service=vllm-dspark"
)"
local_status=$?
set -e
if ((local_status != 0)); then
  echo "Head rank Docker check failed with status ${local_status}." >&2
  exit 1
fi
local_ids=()
if [[ -n "${local_id_output}" ]]; then
  mapfile -t local_ids <<<"${local_id_output}"
fi
if ((${#local_ids[@]} != 1)); then
  echo "Head rank is not running: expected one scoped container, found ${#local_ids[@]}." >&2
  exit 1
fi

remote_script="$(cat <<'REMOTE'
set -euo pipefail
project="$1"
mapfile -t ids < <(
  sudo -n docker ps -q \
    --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.service=vllm-dspark"
)
if ((${#ids[@]} != 1)); then
  echo "Worker rank is not running: expected one scoped container, found ${#ids[@]}." >&2
  exit 1
fi
REMOTE
)"

timeout --kill-after=2 "${rank_timeout}" \
  ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
    timeout --kill-after=2 "${rank_timeout}" \
      bash -s -- "${MIA_PROJECT_NAME}" <<<"${remote_script}"
