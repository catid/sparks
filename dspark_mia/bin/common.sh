#!/usr/bin/env bash

MIA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
requested_env_file="${MIA_ENV_FILE:-mia.env}"
case "${requested_env_file}" in
  /*) candidate_env_file="${requested_env_file}" ;;
  *) candidate_env_file="${MIA_ROOT}/${requested_env_file}" ;;
esac

if [[ ! -f "${candidate_env_file}" || -L "${candidate_env_file}" ]]; then
  echo "Profile must be a regular, non-symlink file: ${candidate_env_file}" >&2
  exit 2
fi
MIA_ENV_FILE="$(readlink -f -- "${candidate_env_file}")"
if [[ "$(dirname "${MIA_ENV_FILE}")" != "${MIA_ROOT}" ]]; then
  echo "Profile must be directly inside ${MIA_ROOT}: ${MIA_ENV_FILE}" >&2
  exit 2
fi
MIA_ENV_BASENAME="$(basename "${MIA_ENV_FILE}")"
if [[ ! "${MIA_ENV_BASENAME}" =~ ^[A-Za-z0-9._-]+\.env$ ]]; then
  echo "Profile basename must end in .env and contain only safe characters." >&2
  exit 2
fi
canonical_env_file="${MIA_ENV_FILE}"
canonical_env_basename="${MIA_ENV_BASENAME}"
export MIA_ENV_FILE MIA_ENV_BASENAME

# shellcheck disable=SC2034  # Shared library variables are consumed by callers.
MIA_UPSTREAM_COMPOSE="${MIA_ROOT}/upstream/docker-compose.dspark.yml"
# shellcheck disable=SC2034
MIA_OVERRIDE_COMPOSE="${MIA_ROOT}/compose.mia.override.yml"
# shellcheck disable=SC2034
MIA_MODEL_LOCK="${MIA_ROOT}/MODEL.lock.json"
# shellcheck disable=SC2034
MIA_UPSTREAM_LOCK="${MIA_ROOT}/UPSTREAM.lock"

set -a
# shellcheck disable=SC1090
source "${MIA_ENV_FILE}"
set +a
# A selected profile may configure runtime values, but it cannot redirect its
# own identity after the containment check.
MIA_ENV_FILE="${canonical_env_file}"
MIA_ENV_BASENAME="${canonical_env_basename}"
export MIA_ENV_FILE MIA_ENV_BASENAME

# An inherited scalar GID must never leak into this multi-HCA profile.
unset NCCL_IB_GID_INDEX

: "${MIA_PROJECT_NAME:?missing MIA_PROJECT_NAME}"
: "${WORKER_HOST:?missing WORKER_HOST}"
: "${WORKER_INSTALL_DIR:?missing WORKER_INSTALL_DIR}"
: "${CLUSTER_SSH_KEY:?missing CLUSTER_SSH_KEY}"
: "${DSPARK_VLLM_IMAGE:?missing DSPARK_VLLM_IMAGE}"
: "${DSPARK_MODEL_HOST_PATH:?missing DSPARK_MODEL_HOST_PATH}"
: "${DSPARK_MODEL:?missing DSPARK_MODEL}"
: "${SERVED_MODEL_NAME:?missing SERVED_MODEL_NAME}"

# shellcheck disable=SC2034  # Shared library array is consumed by callers.
MIA_SSH_OPTIONS=(
  -i "${CLUSTER_SSH_KEY}"
  -o IdentityAgent=none
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o ConnectTimeout=10
)

need_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 2
  fi
}

served_model_ids() {
  local aliases=()
  printf '%s\n' "${SERVED_MODEL_NAME}"
  if [[ -n "${SERVED_MODEL_ALIASES:-}" ]]; then
    read -r -a aliases <<<"${SERVED_MODEL_ALIASES}"
    printf '%s\n' "${aliases[@]}"
  fi
}

missing_served_model_ids() {
  local catalog="$1"
  local model_id
  while IFS= read -r model_id; do
    if ! jq -e --arg id "${model_id}" \
      'any(.data[]?; .id == $id)' <<<"${catalog}" >/dev/null 2>&1; then
      printf '%s\n' "${model_id}"
    fi
  done < <(served_model_ids)
}

require_ssh_identity() {
  if [[ ! -f "${CLUSTER_SSH_KEY}" ]]; then
    echo "Missing cluster SSH identity: ${CLUSTER_SSH_KEY}" >&2
    exit 2
  fi
}

acquire_lifecycle_locks() {
  if [[ "${MIA_SUPERVISOR_CHILD:-0}" == "1" ]]; then
    return 0
  fi

  need_command flock
  local default_lock_dir lifecycle_lock_dir
  default_lock_dir="${HOME}/.local/state/dgx-spark-dspark-mia-locks"
  lifecycle_lock_dir="${MIA_SUPERVISOR_LOCK_DIR:-${MIA_SUPERVISOR_RUNTIME_DIR:-${default_lock_dir}}}"
  if [[ -L "${lifecycle_lock_dir}" ]]; then
    echo "Lifecycle lock directory cannot be a symlink: ${lifecycle_lock_dir}" >&2
    return 2
  fi
  mkdir -p -- "${lifecycle_lock_dir}"
  chmod 0700 -- "${lifecycle_lock_dir}"

  exec {MIA_LIFECYCLE_SUPERVISOR_FD}>"${lifecycle_lock_dir}/supervisor.lock"
  if ! flock -n "${MIA_LIFECYCLE_SUPERVISOR_FD}"; then
    echo "The DSpark supervisor is active; use systemctl for lifecycle changes." >&2
    return 75
  fi
  exec {MIA_LIFECYCLE_RECOVERY_FD}>"${lifecycle_lock_dir}/recovery.lock"
  flock -x "${MIA_LIFECYCLE_RECOVERY_FD}"
}

remote_profile_path() {
  printf '%s/%s\n' "${WORKER_INSTALL_DIR}" "${MIA_ENV_BASENAME}"
}

remote_profile_assignment() {
  printf 'MIA_ENV_FILE=%q' "$(remote_profile_path)"
}

node_host_ip() {
  case "$1" in
    0) printf '%s\n' "${VLLM_HOST_IP}" ;;
    1) printf '%s\n' "${WORKER_VLLM_HOST_IP}" ;;
    *)
      echo "Node rank must be 0 or 1." >&2
      return 2
      ;;
  esac
}

node_headless() {
  case "$1" in
    0) printf '\n' ;;
    1) printf '1\n' ;;
    *)
      echo "Node rank must be 0 or 1." >&2
      return 2
      ;;
  esac
}
