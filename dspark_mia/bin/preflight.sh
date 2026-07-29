#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote commands intentionally use pinned local values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command docker
need_command ssh
need_command ss
need_command sudo
require_ssh_identity

if [[ "$(/usr/bin/hostname -s)" != "spark1" ]]; then
  echo "Run the two-node preflight from spark1." >&2
  exit 2
fi

remote_profile_env="$(remote_profile_assignment)"
"${MIA_ROOT}/bin/validate-static.sh"
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "env ${remote_profile_env} '${WORKER_INSTALL_DIR}/bin/validate-static.sh'"

# Reuse the production four-rail readiness gate verbatim on both hosts.
CX7_LOCAL_SUFFIX=10 \
  "${MIA_ROOT}/../bin/wait-cx7-ready.sh" --check-once
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "CX7_LOCAL_SUFFIX=11 '${WORKER_INSTALL_DIR}/../bin/wait-cx7-ready.sh' --check-once"

# The selected profile supplies its own isolated API and rendezvous ports.
# shellcheck disable=SC2016  # Child shell expands port/hostname at check time.
printf -v listener_check '%s\n' \
  "for port in ${VLLM_PORT} ${MASTER_PORT}; do" \
  '  if ss -ltn "( sport = :${port} )" | tail -n +2 | grep -q .; then' \
  '    echo "Port ${port} is already in use on $(hostname -s)." >&2' \
  '    exit 1' \
  '  fi' \
  'done'
bash -c "${listener_check}"
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "bash -c $(printf '%q' "${listener_check}")"

# This experiment is port-isolated but not GPU-memory-isolated. Do not stop or
# mutate the existing 8000 services implicitly; require an intentional stop.
if pgrep -af '[v]llm (serve|entrypoints)' >/dev/null; then
  echo "A vLLM workload is active on spark1. Stop it explicitly before this trial." >&2
  exit 1
fi
if ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "pgrep -af '[v]llm (serve|entrypoints)' >/dev/null"; then
  echo "A vLLM workload is active on spark2. Stop it explicitly before this trial." >&2
  exit 1
fi

sudo -n docker image inspect "${DSPARK_VLLM_IMAGE}" >/dev/null || {
  echo "Pinned image is not local on spark1; preflight never pulls it." >&2
  exit 1
}
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "sudo -n docker image inspect '${DSPARK_VLLM_IMAGE}' >/dev/null" || {
  echo "Pinned image is not local on spark2; preflight never pulls it." >&2
  exit 1
}

"${MIA_ROOT}/bin/validate-model.sh"
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "env ${remote_profile_env} '${WORKER_INSTALL_DIR}/bin/validate-model.sh'"

echo "Two-node preflight passed; no service was started or changed."
