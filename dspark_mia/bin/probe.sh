#!/usr/bin/env bash
# shellcheck disable=SC2029  # Remote command uses validated profile values.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${script_dir}/common.sh"

need_command curl
need_command docker
need_command jq
need_command ssh
need_command sudo
need_command timeout
require_ssh_identity

if [[ "$(/usr/bin/hostname -s)" != "spark1" ]]; then
  echo "Run the two-node health probe from spark1." >&2
  exit 2
fi

api_timeout="${MIA_HEALTH_API_TIMEOUT_SECONDS:-5}"
ssh_timeout="${MIA_HEALTH_SSH_TIMEOUT_SECONDS:-5}"
docker_timeout="${MIA_HEALTH_DOCKER_TIMEOUT_SECONDS:-10}"
if [[ ! "${api_timeout}" =~ ^[1-9][0-9]*$ ||
      ! "${ssh_timeout}" =~ ^[1-9][0-9]*$ ||
      ! "${docker_timeout}" =~ ^[1-9][0-9]*$ ]]; then
  echo "Health timeouts must be positive integer seconds." >&2
  exit 2
fi
remote_timeout=$((ssh_timeout + docker_timeout + 5))

rank_inspect_format='{{.Id}}|{{.State.StartedAt}}|{{.State.Running}}|{{.State.OOMKilled}}'
set +e
local_id_output="$(
  timeout --kill-after=2 "${docker_timeout}" \
    sudo -n docker ps -aq \
      --filter "label=com.docker.compose.project=${MIA_PROJECT_NAME}" \
      --filter "label=com.docker.compose.service=vllm-dspark"
)"
local_ps_status=$?
set -e
if ((local_ps_status != 0)); then
  echo "local rank unobservable: docker_status=${local_ps_status}" >&2
  exit 10
fi
local_ids=()
if [[ -n "${local_id_output}" ]]; then
  mapfile -t local_ids <<<"${local_id_output}"
fi
if ((${#local_ids[@]} != 1)); then
  echo "local rank unhealthy: expected one scoped container, found ${#local_ids[@]}" >&2
  exit 10
fi
local_state="$(
  timeout --kill-after=2 "${docker_timeout}" \
    sudo -n docker inspect --format "${rank_inspect_format}" \
      "${local_ids[0]}" 2>/dev/null
)" || {
  echo "local rank unhealthy: container inspection failed" >&2
  exit 10
}
IFS='|' read -r local_id local_started local_running local_oom <<<"${local_state}"
if [[ "${local_running}" != "true" || "${local_oom}" != "false" ]]; then
  echo "local rank unhealthy: running=${local_running} oom_killed=${local_oom}" >&2
  exit 10
fi
local_boot_id="$(</proc/sys/kernel/random/boot_id)"

api_base="http://127.0.0.1:${VLLM_PORT}"
api_probe_error=""
models=""
check_api() {
  if ! curl -fsS --max-time "${api_timeout}" "${api_base}/health" >/dev/null; then
    api_probe_error="rank 0 API unhealthy: ${api_base}/health"
    return 1
  fi
  set +e
  models="$(curl -fsS --max-time "${api_timeout}" "${api_base}/v1/models")"
  models_status=$?
  set -e
  if ((models_status != 0)); then
    api_probe_error="rank 0 model listing unavailable: ${api_base}/v1/models"
    return 1
  fi
  mapfile -t missing_model_ids < <(missing_served_model_ids "${models}")
  if ((${#missing_model_ids[@]} != 0)); then
    api_probe_error="rank 0 API is missing required model IDs: ${missing_model_ids[*]}"
    return 1
  fi
  return 0
}

remote_script="$(cat <<'REMOTE'
set -euo pipefail
project="$1"
format='{{.Id}}|{{.State.StartedAt}}|{{.State.Running}}|{{.State.OOMKilled}}'
mapfile -t ids < <(
  sudo -n docker ps -aq \
    --filter "label=com.docker.compose.project=${project}" \
    --filter "label=com.docker.compose.service=vllm-dspark"
)
if ((${#ids[@]} != 1)); then
  echo "remote rank unhealthy: expected one scoped container, found ${#ids[@]}" >&2
  exit 42
fi
state="$(sudo -n docker inspect --format "${format}" "${ids[0]}" 2>/dev/null)" || {
  echo "remote rank unhealthy: container inspection failed" >&2
  exit 42
}
IFS='|' read -r id started running oom <<<"${state}"
if [[ "${running}" != "true" || "${oom}" != "false" ]]; then
  echo "remote rank unhealthy: running=${running} oom_killed=${oom}" >&2
  exit 42
fi
printf '%s|%s|%s\n' "$(< /proc/sys/kernel/random/boot_id)" "${id}" "${started}"
REMOTE
)"

set +e
remote_state="$(
  timeout --kill-after=2 "${remote_timeout}" \
    ssh \
      -i "${CLUSTER_SSH_KEY}" \
      -o IdentityAgent=none \
      -o IdentitiesOnly=yes \
      -o BatchMode=yes \
      -o ConnectTimeout="${ssh_timeout}" \
      -o ServerAliveInterval="${ssh_timeout}" \
      -o ServerAliveCountMax=1 \
      "${WORKER_HOST}" \
      timeout --kill-after=2 "${docker_timeout}" \
        bash -s -- "${MIA_PROJECT_NAME}" <<<"${remote_script}" 2>&1
)"
remote_status=$?
set -e
if ((remote_status != 0)); then
  if ((remote_status == 42)); then
    echo "${remote_state}" >&2
    exit 12
  fi
  if check_api; then
    echo "remote rank temporarily unobservable while the model API remains healthy: ssh_status=${remote_status} ${remote_state}" >&2
    exit 16
  fi
  echo "remote rank unobservable and ${api_probe_error}: ssh_status=${remote_status} ${remote_state}" >&2
  exit 11
fi
IFS='|' read -r remote_boot_id remote_id remote_started <<<"${remote_state}"
if [[ -z "${remote_boot_id}" || -z "${remote_id}" || -z "${remote_started}" ]]; then
  echo "remote rank unhealthy: incomplete identity response" >&2
  exit 12
fi

if ! check_api; then
  echo "${api_probe_error}" >&2
  exit 13
fi

printf 'FINGERPRINT=%s|%s|%s|%s|%s|%s\n' \
  "${local_boot_id}" "${local_id}" "${local_started}" \
  "${remote_boot_id}" "${remote_id}" "${remote_started}"
