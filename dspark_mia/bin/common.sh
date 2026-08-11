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

# Runtime fabric addresses are deliberately not profile data. Preserve an
# explicit tuple passed by the coordinator to a child or remote command, but
# reject any profile that restores the old persisted-IP design.
had_runtime_master_addr="${MASTER_ADDR+x}"
had_runtime_vllm_host_ip="${VLLM_HOST_IP+x}"
had_runtime_worker_vllm_host_ip="${WORKER_VLLM_HOST_IP+x}"
inherited_runtime_master_addr="${MASTER_ADDR-}"
inherited_runtime_vllm_host_ip="${VLLM_HOST_IP-}"
inherited_runtime_worker_vllm_host_ip="${WORKER_VLLM_HOST_IP-}"
unset MASTER_ADDR VLLM_HOST_IP WORKER_VLLM_HOST_IP

set -a
# shellcheck disable=SC1090
source "${MIA_ENV_FILE}"
set +a
if [[ -n "${MASTER_ADDR+x}" || -n "${VLLM_HOST_IP+x}" ||
      -n "${WORKER_VLLM_HOST_IP+x}" ]]; then
  echo "Profiles must not persist MASTER_ADDR, VLLM_HOST_IP, or WORKER_VLLM_HOST_IP; render a hostname-authoritative profile." >&2
  exit 2
fi
if [[ -n "${had_runtime_master_addr}" ]]; then
  MASTER_ADDR="${inherited_runtime_master_addr}"
  export MASTER_ADDR
fi
if [[ -n "${had_runtime_vllm_host_ip}" ]]; then
  VLLM_HOST_IP="${inherited_runtime_vllm_host_ip}"
  export VLLM_HOST_IP
fi
if [[ -n "${had_runtime_worker_vllm_host_ip}" ]]; then
  WORKER_VLLM_HOST_IP="${inherited_runtime_worker_vllm_host_ip}"
  export WORKER_VLLM_HOST_IP
fi
# A selected profile may configure runtime values, but it cannot redirect its
# own identity after the containment check.
MIA_ENV_FILE="${canonical_env_file}"
MIA_ENV_BASENAME="${canonical_env_basename}"
export MIA_ENV_FILE MIA_ENV_BASENAME

# Profiles may select an alternate pinned checkpoint lock, but the lock must
# remain a regular JSON file shipped directly beside the integration. This
# keeps local and worker resolution identical and prevents accidental use of
# an unrelated host path.
requested_model_lock="${MIA_MODEL_LOCK}"
case "${requested_model_lock}" in
  /*) candidate_model_lock="${requested_model_lock}" ;;
  *) candidate_model_lock="${MIA_ROOT}/${requested_model_lock}" ;;
esac
if [[ ! -f "${candidate_model_lock}" || -L "${candidate_model_lock}" ]]; then
  echo "Model lock must be a regular, non-symlink file: ${candidate_model_lock}" >&2
  exit 2
fi
MIA_MODEL_LOCK="$(readlink -f -- "${candidate_model_lock}")"
if [[ "$(dirname "${MIA_MODEL_LOCK}")" != "${MIA_ROOT}" ]]; then
  echo "Model lock must be directly inside ${MIA_ROOT}: ${MIA_MODEL_LOCK}" >&2
  exit 2
fi
MIA_MODEL_LOCK_BASENAME="$(basename "${MIA_MODEL_LOCK}")"
if [[ ! "${MIA_MODEL_LOCK_BASENAME}" =~ ^[A-Za-z0-9._-]+\.json$ ]]; then
  echo "Model lock basename must end in .json and contain only safe characters." >&2
  exit 2
fi
export MIA_MODEL_LOCK MIA_MODEL_LOCK_BASENAME

# An inherited scalar GID must never leak into this multi-HCA profile.
unset NCCL_IB_GID_INDEX

: "${MIA_PROJECT_NAME:?missing MIA_PROJECT_NAME}"
: "${HEAD_HOST:?missing HEAD_HOST}"
: "${WORKER_HOST:?missing WORKER_HOST}"
: "${WORKER_INSTALL_DIR:?missing WORKER_INSTALL_DIR}"
: "${CLUSTER_SSH_KEY:?missing CLUSTER_SSH_KEY}"
: "${DSPARK_VLLM_IMAGE:?missing DSPARK_VLLM_IMAGE}"
: "${DSPARK_MODEL_HOST_PATH:?missing DSPARK_MODEL_HOST_PATH}"
: "${DSPARK_MODEL:?missing DSPARK_MODEL}"
: "${SERVED_MODEL_NAME:?missing SERVED_MODEL_NAME}"
: "${HEAD_NCCL_IB_HCA:?missing HEAD_NCCL_IB_HCA}"
: "${WORKER_NCCL_IB_HCA:?missing WORKER_NCCL_IB_HCA}"

if [[ "${HEAD_HOST}" != "cerberus1" || "${WORKER_HOST}" != "cerberus2" ]]; then
  echo "DSpark profiles must use HEAD_HOST=cerberus1 and WORKER_HOST=cerberus2." >&2
  exit 2
fi
MIA_MANAGEMENT_IFACE="enP7s7"
export MIA_MANAGEMENT_IFACE

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

canonical_cluster_role() {
  case "$1" in
    cerberus1|cerebrus1|spark1) printf 'cerberus1\n' ;;
    cerberus2|cerebrus2|spark2) printf 'cerberus2\n' ;;
    cerberus3|cerebrus3|spark3) printf 'cerberus3\n' ;;
    *) return 1 ;;
  esac
}

require_mia_head_host() {
  local short_host role
  short_host="$(/usr/bin/hostname -s)"
  if ! role="$(canonical_cluster_role "${short_host}")" ||
      [[ "${role}" != "cerberus1" ]]; then
    echo "Run this two-node coordinator from cerberus1 (the exact spark1 alias is accepted during migration); current host: ${short_host}." >&2
    return 2
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

valid_ipv4() {
  local address="$1"
  local octet
  local -a octets=()
  [[ "${address}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
  IFS=. read -r -a octets <<<"${address}"
  for octet in "${octets[@]}"; do
    ((10#${octet} <= 255)) || return 1
  done
  [[ "${address}" != "0.0.0.0" && "${address}" != "255.255.255.255" ]]
}

safe_dns_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ||
     "$1" =~ ^[A-Za-z0-9]$ ]] && ! valid_ipv4 "$1"
}

route_device_to() {
  ip -4 route get "$1" 2>/dev/null | awk '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i == "dev" && i < NF) {
          print $(i + 1)
          exit
        }
      }
    }
  '
}

management_interface_ipv4() {
  local -a addresses=()
  mapfile -t addresses < <(
    ip -4 -o address show dev "${MIA_MANAGEMENT_IFACE}" scope global |
      awk '{sub(/\/.*/, "", $4); print $4}'
  )
  if ((${#addresses[@]} != 1)) || ! valid_ipv4 "${addresses[0]:-}"; then
    echo "${MIA_MANAGEMENT_IFACE} must have exactly one global IPv4 address; found ${#addresses[@]}." >&2
    return 1
  fi
  printf '%s\n' "${addresses[0]}"
}

management_dns_candidates() {
  local host="$1"
  local ssh_target=""
  local candidate
  local -A seen=()
  # mDNS is constrained to enP7s7 on the cluster and therefore gives the
  # preferred DHCP-safe canonical address while router-local DNS is migrated.
  local -a candidates=("${host}.local" "${host}" "${host}.lan")

  ssh_target="$(
    ssh "${MIA_SSH_OPTIONS[@]}" -G "${host}" 2>/dev/null |
      awk '$1 == "hostname" {print $2; exit}'
  )" || true
  if [[ -n "${ssh_target}" ]]; then
    candidates+=("${ssh_target}")
  fi
  case "${host}" in
    cerberus1) candidates+=(spark1.lan cerebrus1.lan) ;;
    cerberus2) candidates+=(spark2.lan cerebrus2.lan) ;;
  esac

  for candidate in "${candidates[@]}"; do
    safe_dns_name "${candidate}" || continue
    [[ -z "${seen[${candidate}]:-}" ]] || continue
    seen["${candidate}"]=1
    printf '%s\n' "${candidate}"
  done
}

resolved_management_ipv4s() {
  local host="$1"
  local mode="$2"
  local candidate address route_device
  local -A seen=()

  while IFS= read -r candidate; do
    while IFS= read -r address; do
      valid_ipv4 "${address}" || continue
      [[ -z "${seen[${address}]:-}" ]] || continue
      case "${mode}" in
        local)
          if ! ip -4 -o address show dev "${MIA_MANAGEMENT_IFACE}" scope global |
            awk '{sub(/\/.*/, "", $4); print $4}' |
            grep -Fxq "${address}"; then
            continue
          fi
          ;;
        routed)
          route_device="$(route_device_to "${address}")"
          [[ "${route_device}" == "${MIA_MANAGEMENT_IFACE}" ]] || continue
          ;;
        *)
          echo "Resolution mode must be local or routed." >&2
          return 2
          ;;
      esac
      seen["${address}"]=1
      printf '%s\n' "${address}"
    done < <(
      getent ahostsv4 "${candidate}" 2>/dev/null |
        awk '{print $1}' || true
    )
  done < <(management_dns_candidates "${host}")
}

resolve_tp2_runtime_addresses() {
  local head_ip worker_ip remote_state remote_host remote_ip remote_route_device
  local -a head_candidates=() worker_candidates=()

  need_command getent
  need_command ip
  need_command ssh
  require_ssh_identity

  head_ip="$(management_interface_ipv4)" || return 1
  mapfile -t head_candidates < <(resolved_management_ipv4s "${HEAD_HOST}" local)
  if ! printf '%s\n' "${head_candidates[@]}" | grep -Fxq "${head_ip}"; then
    echo "${HEAD_HOST} and its DNS/SSH transition aliases do not resolve to ${MIA_MANAGEMENT_IFACE} address ${head_ip}." >&2
    return 1
  fi

  mapfile -t worker_candidates < <(
    resolved_management_ipv4s "${WORKER_HOST}" routed
  )
  if ((${#worker_candidates[@]} == 0)); then
    echo "${WORKER_HOST} and its DNS/SSH transition aliases have no IPv4 route through ${MIA_MANAGEMENT_IFACE}." >&2
    return 1
  fi

  remote_state="$(
    ssh "${MIA_SSH_OPTIONS[@]}" "${WORKER_HOST}" \
      bash -s -- "${MIA_MANAGEMENT_IFACE}" "${head_ip}" <<'REMOTE'
set -euo pipefail
iface="$1"
head_ip="$2"
mapfile -t addresses < <(
  ip -4 -o address show dev "${iface}" scope global |
    awk '{sub(/\/.*/, "", $4); print $4}'
)
if ((${#addresses[@]} != 1)); then
  echo "${iface} must have exactly one global IPv4 address on the worker; found ${#addresses[@]}." >&2
  exit 42
fi
route_device="$(
  ip -4 route get "${head_ip}" 2>/dev/null |
    awk 'NR == 1 {for (i = 1; i <= NF; i++) if ($i == "dev") {print $(i + 1); exit}}'
)"
printf '%s|%s|%s\n' "$(hostname -s)" "${addresses[0]}" "${route_device}"
REMOTE
  )" || {
    echo "Could not validate ${WORKER_HOST} management addressing over its canonical SSH alias." >&2
    return 1
  }
  IFS='|' read -r remote_host remote_ip remote_route_device <<<"${remote_state}"
  if [[ "$(canonical_cluster_role "${remote_host}" 2>/dev/null || true)" != "cerberus2" ]]; then
    echo "Canonical worker alias ${WORKER_HOST} reached unexpected host ${remote_host:-unknown}." >&2
    return 1
  fi
  valid_ipv4 "${remote_ip}" || {
    echo "Worker returned an invalid ${MIA_MANAGEMENT_IFACE} address: ${remote_ip:-missing}." >&2
    return 1
  }
  [[ "${remote_route_device}" == "${MIA_MANAGEMENT_IFACE}" ]] || {
    echo "Worker route to head ${head_ip} uses ${remote_route_device:-no interface}, expected ${MIA_MANAGEMENT_IFACE}." >&2
    return 1
  }
  if ! printf '%s\n' "${worker_candidates[@]}" | grep -Fxq "${remote_ip}"; then
    echo "${WORKER_HOST} DNS/SSH transition aliases do not resolve to its ${MIA_MANAGEMENT_IFACE} address ${remote_ip}." >&2
    return 1
  fi
  worker_ip="${remote_ip}"
  [[ "${head_ip}" != "${worker_ip}" ]] || {
    echo "Head and worker management addresses must differ." >&2
    return 1
  }

  printf '%s\n%s\n%s\n' "${head_ip}" "${head_ip}" "${worker_ip}"
  echo "Resolved TP2 management plane from hostnames: ${HEAD_HOST}=${head_ip}, ${WORKER_HOST}=${worker_ip}, interface=${MIA_MANAGEMENT_IFACE}." >&2
}

load_tp2_runtime_addresses() {
  local resolver_dir="$1"
  local resolver="${resolver_dir}/resolve-runtime.sh"
  local output
  local -a addresses=()
  [[ -x "${resolver}" ]] || {
    echo "Missing executable TP2 runtime resolver: ${resolver}" >&2
    return 2
  }
  output="$("${resolver}")" || return $?
  mapfile -t addresses <<<"${output}"
  if ((${#addresses[@]} != 3)) ||
      ! valid_ipv4 "${addresses[0]}" ||
      ! valid_ipv4 "${addresses[1]}" ||
      ! valid_ipv4 "${addresses[2]}" ||
      [[ "${addresses[0]}" != "${addresses[1]}" ||
         "${addresses[0]}" == "${addresses[2]}" ]]; then
    echo "TP2 runtime resolver returned an invalid address tuple." >&2
    return 1
  fi
  MASTER_ADDR="${addresses[0]}"
  VLLM_HOST_IP="${addresses[1]}"
  WORKER_VLLM_HOST_IP="${addresses[2]}"
  export MASTER_ADDR VLLM_HOST_IP WORKER_VLLM_HOST_IP
}

remote_runtime_assignment() {
  : "${MASTER_ADDR:?runtime MASTER_ADDR has not been resolved}"
  : "${WORKER_VLLM_HOST_IP:?runtime worker VLLM_HOST_IP has not been resolved}"
  printf 'MIA_ENV_FILE=%q MASTER_ADDR=%q VLLM_HOST_IP=%q WORKER_VLLM_HOST_IP=%q' \
    "$(remote_profile_path)" "${MASTER_ADDR}" \
    "${WORKER_VLLM_HOST_IP}" "${WORKER_VLLM_HOST_IP}"
}

# Compose still interpolates distributed-address fields for `ps` and `down`,
# although neither operation opens a listener or rendezvous. Documentation
# addresses keep those commands independent of DNS and interface state. They
# are accepted only by node-compose's explicitly non-launch command path.
load_nonlaunch_compose_addresses() {
  local rank="$1"
  MASTER_ADDR="192.0.2.10"
  WORKER_VLLM_HOST_IP="192.0.2.11"
  case "${rank}" in
    0) VLLM_HOST_IP="${MASTER_ADDR}" ;;
    1) VLLM_HOST_IP="${WORKER_VLLM_HOST_IP}" ;;
    *)
      echo "Node rank must be 0 or 1." >&2
      return 2
      ;;
  esac
  export MASTER_ADDR VLLM_HOST_IP WORKER_VLLM_HOST_IP
}

remote_nonlaunch_assignment() {
  printf 'MIA_ENV_FILE=%q MASTER_ADDR=%q VLLM_HOST_IP=%q WORKER_VLLM_HOST_IP=%q' \
    "$(remote_profile_path)" "192.0.2.10" "192.0.2.11" "192.0.2.11"
}

validate_node_runtime_addresses() {
  local rank="$1"
  local actual_ip route_device
  if [[ "${rank}" != "0" && "${rank}" != "1" ]]; then
    echo "Node rank must be 0 or 1." >&2
    return 2
  fi
  : "${MASTER_ADDR:?runtime MASTER_ADDR has not been supplied}"
  : "${VLLM_HOST_IP:?runtime VLLM_HOST_IP has not been supplied}"
  if ! valid_ipv4 "${MASTER_ADDR}" || ! valid_ipv4 "${VLLM_HOST_IP}"; then
    echo "Runtime MASTER_ADDR and VLLM_HOST_IP must be IPv4 addresses." >&2
    return 1
  fi
  actual_ip="$(management_interface_ipv4)" || return 1
  [[ "${VLLM_HOST_IP}" == "${actual_ip}" ]] || {
    echo "Runtime VLLM_HOST_IP=${VLLM_HOST_IP} does not match ${MIA_MANAGEMENT_IFACE}=${actual_ip}." >&2
    return 1
  }
  if [[ "${rank}" == "0" ]]; then
    [[ "${MASTER_ADDR}" == "${VLLM_HOST_IP}" ]] || {
      echo "Rank 0 MASTER_ADDR must equal its ${MIA_MANAGEMENT_IFACE} VLLM_HOST_IP." >&2
      return 1
    }
  else
    [[ "${MASTER_ADDR}" != "${VLLM_HOST_IP}" ]] || {
      echo "Rank 1 MASTER_ADDR must differ from its VLLM_HOST_IP." >&2
      return 1
    }
    route_device="$(route_device_to "${MASTER_ADDR}")"
    [[ "${route_device}" == "${MIA_MANAGEMENT_IFACE}" ]] || {
      echo "Rank 1 route to MASTER_ADDR=${MASTER_ADDR} uses ${route_device:-no interface}, expected ${MIA_MANAGEMENT_IFACE}." >&2
      return 1
    }
  fi
}

node_nccl_hca() {
  case "$1" in
    0) printf '%s\n' "${HEAD_NCCL_IB_HCA}" ;;
    1) printf '%s\n' "${WORKER_NCCL_IB_HCA}" ;;
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
