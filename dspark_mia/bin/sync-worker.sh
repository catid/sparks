#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command git
need_command rsync
need_command sha256sum
need_command ssh
require_ssh_identity

readiness_helper="${MIA_ROOT}/../bin/wait-cx7-ready.sh"
remote_repo_root="$(dirname -- "${WORKER_INSTALL_DIR}")"
remote_readiness_helper="${remote_repo_root}/bin/wait-cx7-ready.sh"
[[ -f "${readiness_helper}" && ! -L "${readiness_helper}" ]] || {
  echo "Missing regular four-rail readiness helper: ${readiness_helper}" >&2
  exit 1
}

[[ -z "$(git -C "${MIA_ROOT}/upstream" status --porcelain)" ]] || {
  echo "Refusing to sync a modified upstream checkout." >&2
  exit 1
}

ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "mkdir -p '$(printf '%q' "${WORKER_INSTALL_DIR}")' \
    '$(printf '%q' "${remote_repo_root}/bin")'"
rsync_ssh="ssh"
for option in "${MIA_SSH_OPTIONS[@]}"; do
  printf -v rsync_ssh '%s %q' "${rsync_ssh}" "${option}"
done
rsync -a \
  --exclude='logs/' \
  -e "${rsync_ssh}" \
  "${MIA_ROOT}/" \
  "${WORKER_HOST}:${WORKER_INSTALL_DIR}/"
rsync -a \
  -e "${rsync_ssh}" \
  "${readiness_helper}" \
  "${WORKER_HOST}:${remote_readiness_helper}"

expected_commit="$(git -C "${MIA_ROOT}/upstream" rev-parse HEAD)"
local_profile_sha="$(sha256sum "${MIA_ENV_FILE}" | awk '{print $1}')"
local_readiness_sha="$(sha256sum "${readiness_helper}" | awk '{print $1}')"
remote_profile="$(remote_profile_path)"
remote_commit="$(
  ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
    "git -C '$(printf '%q' "${WORKER_INSTALL_DIR}")/upstream' rev-parse HEAD"
)"
[[ "${remote_commit}" == "${expected_commit}" ]] || {
  echo "Worker upstream commit=${remote_commit}, expected=${expected_commit}" >&2
  exit 1
}

remote_profile_sha="$(
  ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
    "sha256sum '$(printf '%q' "${remote_profile}")' | awk '{print \$1}'"
)"
[[ "${remote_profile_sha}" == "${local_profile_sha}" ]] || {
  echo "Worker profile ${MIA_ENV_BASENAME} does not match the selected local file." >&2
  exit 1
}

remote_readiness_sha="$(
  ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
    "sha256sum '$(printf '%q' "${remote_readiness_helper}")' | awk '{print \$1}'"
)"
[[ "${remote_readiness_sha}" == "${local_readiness_sha}" ]] || {
  echo "Worker four-rail readiness helper does not match Spark 1." >&2
  exit 1
}

ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "test -z \"\$(git -C '$(printf '%q' "${WORKER_INSTALL_DIR}")/upstream' status --porcelain)\""

echo "Pinned integration, profile ${MIA_ENV_BASENAME}, and rail gate synced to ${WORKER_HOST}:${WORKER_INSTALL_DIR}"
