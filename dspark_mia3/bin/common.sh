#!/usr/bin/env bash

MIA3_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
requested_env_file="${MIA3_ENV_FILE:-mia3.env}"
case "${requested_env_file}" in
  /*) candidate_env_file="${requested_env_file}" ;;
  *) candidate_env_file="${MIA3_ROOT}/${requested_env_file}" ;;
esac
[[ -f "${candidate_env_file}" && ! -L "${candidate_env_file}" ]] || {
  echo "Trial profile must be a regular, non-symlink file: ${candidate_env_file}" >&2
  exit 2
}
MIA3_ENV_FILE="$(readlink -f -- "${candidate_env_file}")"
[[ "$(dirname -- "${MIA3_ENV_FILE}")" == "${MIA3_ROOT}" ]] || {
  echo "Trial profile must be directly inside ${MIA3_ROOT}." >&2
  exit 2
}
MIA3_ENV_BASENAME="$(basename -- "${MIA3_ENV_FILE}")"
[[ "${MIA3_ENV_BASENAME}" =~ ^[A-Za-z0-9._-]+\.env$ ]] || {
  echo "Trial profile basename must be safe and end in .env." >&2
  exit 2
}
export MIA3_ENV_FILE MIA3_ENV_BASENAME
MIA3_COMPOSE_FILE="${MIA3_ROOT}/compose.yml"
MIA3_MODEL_LOCK="${MIA3_ROOT}/MODEL.lock.json"
MIA3_UPSTREAM_LOCK="${MIA3_ROOT}/UPSTREAM.lock"
# shellcheck disable=SC2034  # Consumed by scripts that source this file.
MIA3_READINESS_HELPER="${MIA3_ROOT}/../bin/wait-cx7-ready.sh"

requested_partition_profile="${MIA3_PARTITION_PROFILE:-default}"
requested_dflash="${MIA3_DFLASH:-}"

if [[ ! "${requested_partition_profile}" =~ ^(default|14-15-14|15-15-13|16-15-12)$ ]]; then
  echo "MIA3_PARTITION_PROFILE must be default, 14-15-14, 15-15-13, or 16-15-12." >&2
  exit 2
fi
MIA3_PARTITION_PROFILE="${requested_partition_profile}"
MIA3_PARTITION_FILE="${MIA3_ROOT}/profiles/${MIA3_PARTITION_PROFILE}.env"
for required_file in \
  "${MIA3_ENV_FILE}" "${MIA3_PARTITION_FILE}" "${MIA3_COMPOSE_FILE}" \
  "${MIA3_MODEL_LOCK}" "${MIA3_UPSTREAM_LOCK}"; do
  if [[ ! -f "${required_file}" || -L "${required_file}" ]]; then
    echo "Required trial input must be a regular, non-symlink file: ${required_file}" >&2
    exit 2
  fi
done

set -a
# shellcheck disable=SC1090
source "${MIA3_ENV_FILE}"
# shellcheck disable=SC1090
source "${MIA3_PARTITION_FILE}"
set +a

for obsolete_name in \
  HEAD_MGMT_IP RANK1_MGMT_IP RANK2_MGMT_IP MASTER_ADDR VLLM_HOST_IP; do
  if grep -Eq "^[[:space:]]*(export[[:space:]]+)?${obsolete_name}=" \
      "${MIA3_ENV_FILE}"; then
    echo "Obsolete numeric management setting in trial profile: ${obsolete_name}" >&2
    exit 2
  fi
done
# A caller's shell may still export values from the old profile. Numeric
# management values are derived below and injected only by node-compose.sh.
unset HEAD_MGMT_IP RANK1_MGMT_IP RANK2_MGMT_IP MASTER_ADDR VLLM_HOST_IP

case "${requested_dflash}" in
  "") ;;
  on|1|true) ENABLE_DSPARK=1 ;;
  off|0|false) ENABLE_DSPARK=0 ;;
  *)
    echo "MIA3_DFLASH must be on or off." >&2
    exit 2
    ;;
esac
export ENABLE_DSPARK MIA3_PARTITION_PROFILE MIA3_PARTITION_FILE

# Never inherit a scalar GID index into a multi-HCA ring configuration.
unset NCCL_IB_GID_INDEX

for required_name in \
  MIA_PROJECT_NAME HEAD_HOST RANK1_HOST RANK2_HOST \
  RANK1_SYNC_HOST RANK2_SYNC_HOST REMOTE_INSTALL_DIR \
  CLUSTER_SSH_KEY MASTER_PORT VLLM_PORT \
  DSPARK_VLLM_IMAGE DSPARK_MODEL_HOST_PATH DSPARK_MODEL \
  HF_CACHE DSPARK_TMP_HOST \
  DSPARK_MODEL_REPO DSPARK_MODEL_REVISION SERVED_MODEL_NAME \
  NCCL_RUNTIME_PATH NCCL_EXPECTED_VERSION \
  TP_SIZE PP_SIZE NNODES MODEL_NUM_LAYERS MODEL_NUM_ATTENTION_HEADS \
  MODEL_NUM_ROUTED_EXPERTS CONTROL_IFACE NCCL_IB_HCA \
  NCCL_IB_SUBNET_AWARE_ROUTING NCCL_NET_PLUGIN CX7_C3_PORT_MAP; do
  if [[ -z "${!required_name:-}" ]]; then
    echo "Missing required setting: ${required_name}" >&2
    exit 2
  fi
done

[[ "${HEAD_HOST}" == cerberus1 && "${RANK1_HOST}" == cerberus2 &&
   "${RANK2_HOST}" == cerberus3 ]] || {
  echo "Mia3 ranks must use canonical cerberus1, cerberus2, and cerberus3 hostnames." >&2
  exit 2
}
[[ "${CONTROL_IFACE}" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "Unsafe control interface in trial profile: ${CONTROL_IFACE}" >&2
  exit 2
}

[[ "${MIA_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  echo "Unsafe Compose project name: ${MIA_PROJECT_NAME}" >&2
  exit 2
}
for cluster_host in "${HEAD_HOST}" "${RANK1_HOST}" "${RANK2_HOST}"; do
  [[ "${cluster_host}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "Unsafe cluster hostname in trial profile: ${cluster_host}" >&2
    exit 2
  }
done
valid_ipv4() {
  local address="$1" octet
  local -a octets=()
  [[ "${address}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
  IFS=. read -r -a octets <<<"${address}"
  for octet in "${octets[@]}"; do
    ((10#${octet} <= 255)) || return 1
  done
}
for cluster_address in "${RANK1_SYNC_HOST}" "${RANK2_SYNC_HOST}"; do
  valid_ipv4 "${cluster_address}" || {
    echo "Invalid cluster IPv4 address in trial profile: ${cluster_address}" >&2
    exit 2
  }
done
for safe_path in "${REMOTE_INSTALL_DIR}" "${CLUSTER_SSH_KEY}" \
  "${DSPARK_MODEL_HOST_PATH}" "${DSPARK_MODEL}" \
  "${HF_CACHE}" "${DSPARK_TMP_HOST}" "${NCCL_RUNTIME_PATH}"; do
  [[ "${safe_path}" =~ ^/[A-Za-z0-9._/@+-]+$ ]] || {
    echo "Unsafe absolute path in trial profile: ${safe_path}" >&2
    exit 2
  }
done
[[ "$(basename -- "${REMOTE_INSTALL_DIR}")" == dspark_mia3 ]] || {
  echo "REMOTE_INSTALL_DIR must end in the dedicated dspark_mia3 directory." >&2
  exit 2
}
normalized_remote_install="$(realpath -m -- "${REMOTE_INSTALL_DIR}")"
[[ "${REMOTE_INSTALL_DIR}" == "${normalized_remote_install}" ]] || {
  echo "REMOTE_INSTALL_DIR must be a normalized absolute path: ${REMOTE_INSTALL_DIR}" >&2
  exit 2
}
cluster_key_dir="$(dirname -- "${CLUSTER_SSH_KEY}")"
[[ "$(basename -- "${cluster_key_dir}")" == .ssh ]] || {
  echo "CLUSTER_SSH_KEY must live in the service account's .ssh directory." >&2
  exit 2
}
remote_service_home="$(dirname -- "${cluster_key_dir}")"
remote_repo_root="$(dirname -- "${REMOTE_INSTALL_DIR}")"
[[ "${remote_repo_root}" == "${remote_service_home}/"* &&
   "${remote_repo_root}" != "${remote_service_home}" ]] || {
  echo "REMOTE_INSTALL_DIR must be a dedicated checkout child under ${remote_service_home}." >&2
  exit 2
}
IFS=/ read -r -a remote_path_parts <<<"${REMOTE_INSTALL_DIR}"
(( ${#remote_path_parts[@]} >= 5 )) || {
  echo "REMOTE_INSTALL_DIR is too broad for a delete-scoped sync: ${REMOTE_INSTALL_DIR}" >&2
  exit 2
}
for port in "${MASTER_PORT}" "${VLLM_PORT}"; do
  if [[ ! "${port}" =~ ^[1-9][0-9]{1,4}$ ]] || ((10#${port} > 65535)); then
    echo "Invalid TCP port in trial profile: ${port}" >&2
    exit 2
  fi
done

MIA3_SSH_OPTIONS=(
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

require_ssh_identity() {
  if [[ ! -f "${CLUSTER_SSH_KEY}" || -L "${CLUSTER_SSH_KEY}" ]]; then
    echo "Missing regular cluster SSH identity: ${CLUSTER_SSH_KEY}" >&2
    exit 2
  fi
  local mode
  mode="$(stat -c '%a' "${CLUSTER_SSH_KEY}")"
  if ((8#${mode} & 8#077)); then
    echo "Cluster SSH identity must not be group/world accessible: ${CLUSTER_SSH_KEY}" >&2
    exit 2
  fi
}

require_head_host() {
  local actual
  actual="$(hostname -s)"
  if [[ "${actual}" != "${HEAD_HOST}" ]]; then
    echo "Run this command from ${HEAD_HOST}; current host is ${actual}." >&2
    exit 2
  fi
}

rank_host() {
  case "$1" in
    0) printf '%s\n' "${HEAD_HOST}" ;;
    1) printf '%s\n' "${RANK1_HOST}" ;;
    2) printf '%s\n' "${RANK2_HOST}" ;;
    *) echo "Rank must be 0, 1, or 2." >&2; return 2 ;;
  esac
}

canonical_cluster_role() {
  case "$1" in
    cerberus1|cerebrus1|spark1) printf 'cerberus1\n' ;;
    cerberus2|cerebrus2|spark2) printf 'cerberus2\n' ;;
    cerberus3|cerebrus3|spark3) printf 'cerberus3\n' ;;
    *) return 1 ;;
  esac
}

declare -a MIA3_CONTROL_IPV4S=()
declare -a MIA3_RUNTIME_MGMT_IPV4S=()
declare -a MIA3_RUNTIME_MGMT_NAMES=()
MIA3_MANAGEMENT_RESOLVED=0
MIA3_LOOKUP_IPV4=""

address_is_on_control_iface() {
  local expected="$1" address
  for address in "${MIA3_CONTROL_IPV4S[@]}"; do
    [[ "${address}" == "${expected}" ]] && return 0
  done
  return 1
}

lookup_unique_management_ipv4() {
  local dns_name="$1" output address
  local -a addresses=()
  MIA3_LOOKUP_IPV4=""
  if ! output="$(getent ahostsv4 "${dns_name}" 2>/dev/null)"; then
    return 3
  fi
  mapfile -t addresses < <(
    awk '{print $1}' <<<"${output}" | sort -u
  )
  ((${#addresses[@]} > 0)) || return 3
  for address in "${addresses[@]}"; do
    valid_ipv4 "${address}" || {
      echo "${dns_name} returned an invalid IPv4 address: ${address}" >&2
      return 2
    }
  done
  if ((${#addresses[@]} != 1)); then
    echo "${dns_name} returned ambiguous IPv4 addresses: ${addresses[*]}" >&2
    return 2
  fi
  MIA3_LOOKUP_IPV4="${addresses[0]}"
}

validate_management_route() {
  local rank="$1" dns_name="$2" address="$3"
  local actual_role="" expected_role route route_dev route_source
  expected_role="$(rank_host "${rank}")"
  actual_role="$(canonical_cluster_role "$(hostname -s)" 2>/dev/null || true)"

  if address_is_on_control_iface "${address}"; then
    if [[ -z "${actual_role}" || "${actual_role}" != "${expected_role}" ]]; then
      echo "${dns_name} unexpectedly resolves to this host's ${CONTROL_IFACE} address ${address}." >&2
      return 1
    fi
    return 0
  fi
  if [[ "${actual_role}" == "${expected_role}" ]]; then
    echo "${dns_name} resolves to ${address}, which is not owned by local ${CONTROL_IFACE}." >&2
    return 1
  fi

  route="$(ip -4 route get "${address}" 2>/dev/null | head -n 1)"
  route_dev="$(awk '{for (i=1; i<NF; i++) if ($i == "dev") {print $(i+1); exit}}' <<<"${route}")"
  route_source="$(awk '{for (i=1; i<NF; i++) if ($i == "src") {print $(i+1); exit}}' <<<"${route}")"
  if [[ "${route_dev}" != "${CONTROL_IFACE}" ]]; then
    echo "${dns_name} (${address}) routes via ${route_dev:-none}, not ${CONTROL_IFACE}." >&2
    return 1
  fi
  if ! address_is_on_control_iface "${route_source}"; then
    echo "${dns_name} (${address}) uses source ${route_source:-none}, not a ${CONTROL_IFACE} IPv4." >&2
    return 1
  fi
}

resolve_rank_management_ipv4() {
  local rank="$1" canonical legacy spark dns_name lookup_status index
  local -a candidates=()
  canonical="$(rank_host "${rank}")"
  legacy="${canonical/cerberus/cerebrus}"
  spark="${canonical/cerberus/spark}"
  candidates=(
    "${canonical}.local"
    "${canonical}.lan"
    "${legacy}.lan"
    "${spark}.lan"
    "${legacy}.local"
    "${spark}.local"
  )

  for index in "${!candidates[@]}"; do
    dns_name="${candidates[${index}]}"
    if lookup_unique_management_ipv4 "${dns_name}"; then
      validate_management_route "${rank}" "${dns_name}" "${MIA3_LOOKUP_IPV4}" || return 1
      MIA3_RUNTIME_MGMT_IPV4S[rank]="${MIA3_LOOKUP_IPV4}"
      MIA3_RUNTIME_MGMT_NAMES[rank]="${dns_name}"
      if ((index > 1)); then
        echo "WARNING: canonical DNS is unavailable for ${canonical}; using transitional ${dns_name}." >&2
      fi
      return 0
    else
      lookup_status=$?
      ((lookup_status == 3)) || return "${lookup_status}"
    fi
  done
  echo "Cannot resolve ${canonical} on management DNS or an explicit transitional alias." >&2
  return 1
}

resolve_management_plane() {
  local rank address
  ((MIA3_MANAGEMENT_RESOLVED == 0)) || return 0
  need_command getent
  need_command hostname
  need_command ip
  mapfile -t MIA3_CONTROL_IPV4S < <(
    ip -4 -o address show dev "${CONTROL_IFACE}" scope global |
      awk '{split($4, value, "/"); print value[1]}' | sort -u
  )
  ((${#MIA3_CONTROL_IPV4S[@]} > 0)) || {
    echo "${CONTROL_IFACE} has no global IPv4 address." >&2
    return 1
  }
  for address in "${MIA3_CONTROL_IPV4S[@]}"; do
    valid_ipv4 "${address}" || {
      echo "${CONTROL_IFACE} returned an invalid IPv4 address: ${address}" >&2
      return 1
    }
  done
  for rank in 0 1 2; do
    resolve_rank_management_ipv4 "${rank}" || return
  done
  [[ "${MIA3_RUNTIME_MGMT_IPV4S[0]}" != "${MIA3_RUNTIME_MGMT_IPV4S[1]}" &&
     "${MIA3_RUNTIME_MGMT_IPV4S[0]}" != "${MIA3_RUNTIME_MGMT_IPV4S[2]}" &&
     "${MIA3_RUNTIME_MGMT_IPV4S[1]}" != "${MIA3_RUNTIME_MGMT_IPV4S[2]}" ]] || {
    echo "Resolved management addresses must be distinct." >&2
    return 1
  }
  MIA3_MANAGEMENT_RESOLVED=1
}

rank_runtime_ipv4() {
  case "$1" in
    0|1|2) ;;
    *) echo "Rank must be 0, 1, or 2." >&2; return 2 ;;
  esac
  resolve_management_plane || return
  printf '%s\n' "${MIA3_RUNTIME_MGMT_IPV4S[$1]}"
}

rank_runtime_management_name() {
  case "$1" in
    0|1|2) ;;
    *) echo "Rank must be 0, 1, or 2." >&2; return 2 ;;
  esac
  resolve_management_plane || return
  printf '%s\n' "${MIA3_RUNTIME_MGMT_NAMES[$1]}"
}

rank_sync_host() {
  case "$1" in
    1) printf '%s\n' "${RANK1_SYNC_HOST}" ;;
    2) printf '%s\n' "${RANK2_SYNC_HOST}" ;;
    *) echo "Bulk sync is valid only for worker rank 1 or 2." >&2; return 2 ;;
  esac
}

rank_headless() {
  case "$1" in
    0) printf '\n' ;;
    1|2) printf '1\n' ;;
    *) echo "Rank must be 0, 1, or 2." >&2; return 2 ;;
  esac
}

shell_join() {
  local result="" item quoted
  for item in "$@"; do
    printf -v quoted '%q' "${item}"
    result+="${result:+ }${quoted}"
  done
  printf '%s\n' "${result}"
}

ssh_command() {
  local host="$1"
  shift
  local remote_command
  host="$(management_ssh_host "${host}")"
  remote_command="$(shell_join "$@")"
  ssh "${MIA3_SSH_OPTIONS[@]}" "${host}" "${remote_command}"
}

management_ssh_host() {
  local host="$1" role
  role="$(canonical_cluster_role "${host}" 2>/dev/null || true)"
  if [[ -n "${role}" ]]; then
    printf '%s.local\n' "${role}"
  else
    printf '%s\n' "${host}"
  fi
}

remote_trial_command() {
  local rank="$1" script_name="$2"
  shift 2
  local host
  host="$(rank_host "${rank}")"
  ssh_command "${host}" env \
    "MIA3_ENV_FILE=${MIA3_ENV_BASENAME}" \
    "MIA3_PARTITION_PROFILE=${MIA3_PARTITION_PROFILE}" \
    "MIA3_DFLASH=$([[ "${ENABLE_DSPARK}" == 1 ]] && printf on || printf off)" \
    "${REMOTE_INSTALL_DIR}/bin/${script_name}" "$@"
}

served_model_ids() {
  local aliases=()
  printf '%s\n' "${SERVED_MODEL_NAME}"
  if [[ -n "${SERVED_MODEL_ALIASES:-}" ]]; then
    read -r -a aliases <<<"${SERVED_MODEL_ALIASES}"
    printf '%s\n' "${aliases[@]}"
  fi
}

acquire_lifecycle_lock() {
  need_command flock
  local lock_dir="${HOME}/.local/state/dgx-spark-mia3"
  if [[ -L "${lock_dir}" ]]; then
    echo "Lifecycle lock directory cannot be a symlink: ${lock_dir}" >&2
    exit 2
  fi
  mkdir -p -- "${lock_dir}"
  chmod 0700 -- "${lock_dir}"
  exec {MIA3_LIFECYCLE_FD}>"${lock_dir}/lifecycle.lock"
  if ! flock -n "${MIA3_LIFECYCLE_FD}"; then
    echo "Another mia3 lifecycle operation is active." >&2
    exit 75
  fi
}
