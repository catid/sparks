#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command ssh
require_ssh_identity
remote_profile_env="$(remote_profile_assignment)"

echo "== cerebrus1 / rank 0 =="
"${MIA_ROOT}/bin/node-compose.sh" 0 ps -a
echo "== cerebrus2 / rank 1 =="
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "env ${remote_profile_env} '${WORKER_INSTALL_DIR}/bin/node-compose.sh' 1 ps -a"
