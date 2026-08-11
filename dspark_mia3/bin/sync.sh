#!/usr/bin/env bash
# shellcheck disable=SC2029
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command rsync
need_command sha256sum
need_command ssh
require_head_host
require_ssh_identity

"${MIA3_ROOT}/bin/validate-static.sh"
local_digest="$("${MIA3_ROOT}/bin/tree-digest.sh")"
[[ -f "${MIA3_READINESS_HELPER}" && ! -L "${MIA3_READINESS_HELPER}" ]] || {
  echo "Missing regular ring readiness helper: ${MIA3_READINESS_HELPER}" >&2
  exit 1
}
local_readiness_sha="$(sha256sum "${MIA3_READINESS_HELPER}" | awk '{print $1}')"
remote_repo_root="$(dirname -- "${REMOTE_INSTALL_DIR}")"
remote_readiness_helper="${remote_repo_root}/bin/wait-cx7-ready.sh"
remote_sync_sentinel="${REMOTE_INSTALL_DIR}/.mia3-sync-root"

rsync_ssh="ssh"
for option in "${MIA3_SSH_OPTIONS[@]}"; do
  printf -v rsync_ssh '%s %q' "${rsync_ssh}" "${option}"
done

for rank in 2 1; do
  host="$(rank_host "${rank}")"
  transport_host="$(management_ssh_host "${host}")"
  if ssh_command "${host}" test -e "${REMOTE_INSTALL_DIR}"; then
    ssh_command "${host}" test -d "${REMOTE_INSTALL_DIR}"
    ssh_command "${host}" test ! -L "${REMOTE_INSTALL_DIR}"
    if ! ssh_command "${host}" test -f "${remote_sync_sentinel}"; then
      # Adopt only a recognizable prior trial tree. An arbitrary existing
      # directory named dspark_mia3 is never made delete-capable.
      ssh_command "${host}" test -f "${REMOTE_INSTALL_DIR}/compose.yml"
      ssh_command "${host}" test -x "${REMOTE_INSTALL_DIR}/bin/tree-digest.sh"
      ssh_command "${host}" install -m 0600 /dev/null "${remote_sync_sentinel}"
    fi
  else
    ssh_command "${host}" mkdir -p -- "${REMOTE_INSTALL_DIR}"
    ssh_command "${host}" install -m 0600 /dev/null "${remote_sync_sentinel}"
  fi
  ssh_command "${host}" test -f "${remote_sync_sentinel}"
  # --delete is scoped to a basename/depth-validated directory that carries
  # the ownership sentinel above. The sentinel itself is excluded.
  rsync -a --delete \
    --exclude='/.mia3-sync-root' \
    --exclude='logs/' \
    -e "${rsync_ssh}" \
    "${MIA3_ROOT}/" \
    "${transport_host}:${REMOTE_INSTALL_DIR}/"
  remote_digest="$(ssh_command "${host}" "${REMOTE_INSTALL_DIR}/bin/tree-digest.sh")"
  [[ "${remote_digest}" == "${local_digest}" ]] || {
    echo "${host}: synced integration digest differs from ${HEAD_HOST}." >&2
    exit 1
  }
  remote_readiness_sha="$(ssh_command "${host}" sha256sum "${remote_readiness_helper}" | awk '{print $1}')"
  [[ "${remote_readiness_sha}" == "${local_readiness_sha}" ]] || {
    echo "${host}: parent-checkout ring readiness helper differs from ${HEAD_HOST}; synchronize the repository separately." >&2
    exit 1
  }
  remote_trial_command "${rank}" validate-static.sh
  echo "Synced isolated trial to ${host}:${REMOTE_INSTALL_DIR}; shared readiness helper matched read-only."
done
