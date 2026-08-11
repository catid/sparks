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

require_mia_head_host

remote_profile_env="$(remote_profile_assignment)"
"${MIA_ROOT}/bin/validate-static.sh"
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "env ${remote_profile_env} '${WORKER_INSTALL_DIR}/bin/validate-static.sh'"

# Production deliberately uses only the direct cerebrus1-P1 <->
# cerebrus2-P0 edge. A cerebrus3 outage must not block TP2 serving.
CX7_NODE_ROLE=cerebrus1 \
  "${MIA_ROOT}/../bin/wait-cx7-ready.sh" --check-once --scope tp2
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "CX7_NODE_ROLE=cerebrus2 '${WORKER_INSTALL_DIR}/../bin/wait-cx7-ready.sh' --check-once --scope tp2"

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
  echo "A vLLM workload is active on cerebrus1. Stop it explicitly before this trial." >&2
  exit 1
fi
if ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "pgrep -af '[v]llm (serve|entrypoints)' >/dev/null"; then
  echo "A vLLM workload is active on cerebrus2. Stop it explicitly before this trial." >&2
  exit 1
fi

sudo -n docker image inspect "${DSPARK_VLLM_IMAGE}" >/dev/null || {
  echo "Pinned image is not local on cerebrus1; preflight never pulls it." >&2
  exit 1
}
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "sudo -n docker image inspect '${DSPARK_VLLM_IMAGE}' >/dev/null" || {
  echo "Pinned image is not local on cerebrus2; preflight never pulls it." >&2
  exit 1
}

"${MIA_ROOT}/bin/validate-model.sh"
ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
  "env ${remote_profile_env} '${WORKER_INSTALL_DIR}/bin/validate-model.sh'"

echo "Two-node preflight passed; no service was started or changed."
