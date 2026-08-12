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

require_mia_head_host

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
ssh_options=(
  -i "${CLUSTER_SSH_KEY}"
  -o IdentityAgent=none
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout="${ssh_timeout}"
  -o ServerAliveInterval="${ssh_timeout}"
  -o ServerAliveCountMax=1
)
ssh_control_dir="${MIA_SSH_CONTROL_DIR:-}"
if [[ -z "${ssh_control_dir}" && -n "${MIA_SUPERVISOR_RUNTIME_DIR:-}" ]]; then
  ssh_control_dir="${MIA_SUPERVISOR_RUNTIME_DIR}/ssh"
fi
if [[ -n "${ssh_control_dir}" ]]; then
  if [[ ! "${ssh_control_dir}" =~ ^/[A-Za-z0-9._/@+-]+$ ||
        "${ssh_control_dir}" == *'/../'* ||
        "${ssh_control_dir}" == */.. ||
        -L "${ssh_control_dir}" ]]; then
    echo "SSH control directory is unsafe." >&2
    exit 2
  fi
  mkdir -p -- "${ssh_control_dir}"
  chmod 0700 -- "${ssh_control_dir}"
  control_metadata="$(stat -c '%u:%g:%a' -- "${ssh_control_dir}")"
  [[ "${control_metadata}" == "${UID}:$(id -g):700" ]] || {
    echo "SSH control directory must be private and service-owned." >&2
    exit 2
  }
  ssh_options+=(
    -o ControlMaster=auto
    -o ControlPersist=90
    -o "ControlPath=${ssh_control_dir}/worker-%C"
  )
fi

# The supervisor may have been upgraded in place while its shell process keeps
# the old 10-second loop. Cache only a complete generation fingerprint for the
# configured 30-second probe cadence, but still check the HTTP API on every
# invocation. This immediately eliminates most Docker/sudo/SSH churn without
# recycling the loaded model. stop.sh invalidates the cache before any planned
# generation change.
probe_cache_seconds="${MIA_HEALTH_FULL_PROBE_INTERVAL_SECONDS:-30}"
probe_cache_file=""
if [[ ! "${probe_cache_seconds}" =~ ^[1-9][0-9]*$ ]]; then
  echo "MIA_HEALTH_FULL_PROBE_INTERVAL_SECONDS must be a positive integer." >&2
  exit 2
fi
if [[ -n "${MIA_SUPERVISOR_RUNTIME_DIR:-}" &&
      "${MIA_SUPERVISOR_RUNTIME_DIR}" =~ ^/[A-Za-z0-9._/@+-]+$ &&
      ! -L "${MIA_SUPERVISOR_RUNTIME_DIR}" ]]; then
  probe_cache_file="${MIA_SUPERVISOR_RUNTIME_DIR}/last-full-probe"
fi

cached_api_healthy() {
  local cached_models
  curl -fsS --max-time "${api_timeout}" \
    "http://127.0.0.1:${VLLM_PORT}/health" >/dev/null || return 1
  cached_models="$(
    curl -fsS --max-time "${api_timeout}" \
      "http://127.0.0.1:${VLLM_PORT}/v1/models"
  )" || return 1
  mapfile -t cached_missing < <(missing_served_model_ids "${cached_models}")
  ((${#cached_missing[@]} == 0))
}

if [[ -n "${probe_cache_file}" && -f "${probe_cache_file}" &&
      ! -L "${probe_cache_file}" ]]; then
  cache_metadata="$(stat -c '%u:%g:%a:%Y' -- "${probe_cache_file}")"
  IFS=: read -r cache_uid cache_gid cache_mode cache_epoch <<<"${cache_metadata}"
  cache_age=$(($(date +%s) - cache_epoch))
  cached_fingerprint="$(<"${probe_cache_file}")"
  if [[ "${cache_uid}:${cache_gid}:${cache_mode}" == "${UID}:$(id -g):600" &&
        "${cache_age}" -ge 0 && "${cache_age}" -lt "${probe_cache_seconds}" &&
        "${cached_fingerprint}" =~ ^FINGERPRINT=([A-Za-z0-9_.:+-]+\|){5}[A-Za-z0-9_.:+-]+$ ]] &&
     cached_api_healthy; then
    printf '%s\n' "${cached_fingerprint}"
    exit 0
  fi
fi

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
    ssh "${ssh_options[@]}" \
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

fingerprint="$(printf 'FINGERPRINT=%s|%s|%s|%s|%s|%s' \
  "${local_boot_id}" "${local_id}" "${local_started}" \
  "${remote_boot_id}" "${remote_id}" "${remote_started}")"
if [[ -n "${probe_cache_file}" ]]; then
  temporary_cache="${probe_cache_file}.new.$$"
  printf '%s\n' "${fingerprint}" >"${temporary_cache}"
  chmod 0600 "${temporary_cache}"
  mv -f -- "${temporary_cache}" "${probe_cache_file}"
fi
printf '%s\n' "${fingerprint}"
