#!/usr/bin/env bash
# Explicit, non-deleting 166 GB model replication. This is never called by start.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command rsync
need_command ssh
require_head_host
require_ssh_identity
"${MIA3_ROOT}/bin/validate-model.sh"

rsync_ssh="ssh"
for option in "${MIA3_SSH_OPTIONS[@]}"; do
  printf -v rsync_ssh '%s %q' "${rsync_ssh}" "${option}"
done

ranks=("$@")
if ((${#ranks[@]} == 0)); then
  ranks=(2 1)
fi

for rank in "${ranks[@]}"; do
  [[ "${rank}" =~ ^[12]$ ]] || { echo "Usage: $0 [1|2 ...]" >&2; exit 2; }
  host="$(rank_host "${rank}")"
  sync_host="$(rank_sync_host "${rank}")"
  remote_parent="$(dirname -- "${DSPARK_MODEL_HOST_PATH}")"
  ssh_command "${sync_host}" mkdir -p -- "${remote_parent}" "${DSPARK_MODEL_HOST_PATH}"
  rsync -a --partial --human-readable --info=progress2 \
    -e "${rsync_ssh}" \
    "${DSPARK_MODEL_HOST_PATH}/" \
    "${sync_host}:${DSPARK_MODEL_HOST_PATH}/"
  remote_trial_command "${rank}" validate-model.sh
  echo "Pinned model verified on ${host}."
done
