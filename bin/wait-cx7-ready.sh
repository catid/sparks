#!/usr/bin/env bash
set -euo pipefail

# Block a distributed model service until every logical RoCE rail between the
# two DGX Sparks is usable. The two physical ConnectX-7 cables expose four
# logical netdev/RDMA links.

readonly -a cx7_interfaces=(
  enp1s0f0np0
  enP2p1s0f0np0
  enp1s0f1np1
  enP2p1s0f1np1
)
readonly -a cx7_subnets=(100 101 102 103)

expected_mtu="${CX7_EXPECTED_MTU:-9000}"
poll_seconds="${CX7_POLL_SECONDS:-2}"
ping_timeout="${CX7_PING_TIMEOUT_SECONDS:-1}"
timeout_seconds="${CX7_TIMEOUT_SECONDS:-0}"
local_suffix="${CX7_LOCAL_SUFFIX:-}"
action="${1:---wait}"

usage() {
  cat <<'EOF'
Usage: wait-cx7-ready.sh [--wait|--check-once|--describe]

Environment:
  CX7_LOCAL_SUFFIX          10 for Spark 1 or 11 for Spark 2. By default the
                            script derives this from the spark1/spark2 hostname.
  CX7_EXPECTED_MTU          Required MTU on every rail (default: 9000).
  CX7_POLL_SECONDS          Delay between checks (default: 2).
  CX7_PING_TIMEOUT_SECONDS  Per-rail peer ping timeout (default: 1).
  CX7_TIMEOUT_SECONDS       Overall wait timeout; 0 waits forever (default: 0).
EOF
}

require_uint() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s must be an unsigned integer (got %q)\n' "${name}" "${value}" >&2
    exit 2
  fi
}

case "${action}" in
  --wait | --check-once | --describe) ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

require_uint CX7_EXPECTED_MTU "${expected_mtu}"
require_uint CX7_POLL_SECONDS "${poll_seconds}"
require_uint CX7_PING_TIMEOUT_SECONDS "${ping_timeout}"
require_uint CX7_TIMEOUT_SECONDS "${timeout_seconds}"
if ((expected_mtu < 576 || poll_seconds < 1 || ping_timeout < 1)); then
  echo "MTU must be at least 576, and poll/ping intervals must be positive." >&2
  exit 2
fi

if [[ -z "${local_suffix}" ]]; then
  case "$(/usr/bin/hostname -s)" in
    spark1) local_suffix=10 ;;
    spark2) local_suffix=11 ;;
    *)
      echo "Cannot infer Spark role; set CX7_LOCAL_SUFFIX to 10 or 11." >&2
      exit 2
      ;;
  esac
fi

case "${local_suffix}" in
  10) peer_suffix=11 ;;
  11) peer_suffix=10 ;;
  *)
    echo "CX7_LOCAL_SUFFIX must be 10 (Spark 1) or 11 (Spark 2)." >&2
    exit 2
    ;;
esac

describe_layout() {
  local index
  printf 'local_suffix=%s peer_suffix=%s expected_mtu=%s\n' \
    "${local_suffix}" "${peer_suffix}" "${expected_mtu}"
  for index in "${!cx7_interfaces[@]}"; do
    printf '%s local=192.168.%s.%s/24 peer=192.168.%s.%s\n' \
      "${cx7_interfaces[index]}" \
      "${cx7_subnets[index]}" "${local_suffix}" \
      "${cx7_subnets[index]}" "${peer_suffix}"
  done
}

if [[ "${action}" == "--describe" ]]; then
  describe_layout
  exit 0
fi

declare -a check_errors=()

add_error() {
  check_errors+=("$1")
}

check_all_rails() {
  local rdma_links=""
  local index interface subnet local_cidr peer_ip carrier mtu
  check_errors=()

  if ! rdma_links="$(/usr/bin/rdma link show 2>&1)"; then
    add_error "rdma link show failed: ${rdma_links}"
    rdma_links=""
  fi

  for index in "${!cx7_interfaces[@]}"; do
    interface="${cx7_interfaces[index]}"
    subnet="${cx7_subnets[index]}"
    local_cidr="192.168.${subnet}.${local_suffix}/24"
    peer_ip="192.168.${subnet}.${peer_suffix}"

    if [[ ! -d "/sys/class/net/${interface}" ]]; then
      add_error "${interface}: netdev missing"
      continue
    fi

    if ! carrier="$(<"/sys/class/net/${interface}/carrier")"; then
      add_error "${interface}: cannot read carrier state"
      continue
    fi
    if [[ "${carrier}" != "1" ]]; then
      add_error "${interface}: carrier=${carrier}"
    fi

    if ! mtu="$(<"/sys/class/net/${interface}/mtu")"; then
      add_error "${interface}: cannot read MTU"
      continue
    fi
    if [[ "${mtu}" != "${expected_mtu}" ]]; then
      add_error "${interface}: mtu=${mtu}, expected ${expected_mtu}"
    fi

    if ! /usr/bin/ip -4 -o address show dev "${interface}" scope global |
      /usr/bin/awk -v expected="${local_cidr}" \
        '$4 == expected { found = 1 } END { exit(found ? 0 : 1) }'; then
      add_error "${interface}: missing ${local_cidr}"
    fi

    if [[ -n "${rdma_links}" ]] &&
      ! /usr/bin/awk -v expected="${interface}" '
        $3 == "state" && $4 == "ACTIVE" &&
        $5 == "physical_state" && $6 == "LINK_UP" &&
        $7 == "netdev" && $8 == expected { found = 1 }
        END { exit(found ? 0 : 1) }
      ' <<<"${rdma_links}"; then
      add_error "${interface}: RDMA link is not ACTIVE/LINK_UP"
    fi

    if ! /usr/bin/ping -q -n -I "${interface}" -c 1 \
      -W "${ping_timeout}" "${peer_ip}" >/dev/null 2>&1; then
      add_error "${interface}: peer ${peer_ip} unreachable"
    fi
  done

  ((${#check_errors[@]} == 0))
}

print_errors() {
  local item
  for item in "${check_errors[@]}"; do
    printf ' - %s\n' "${item}" >&2
  done
}

if [[ "${action}" == "--check-once" ]]; then
  if check_all_rails; then
    echo "All four CX-7/RoCE rails are ready."
    exit 0
  fi
  echo "CX-7/RoCE readiness check failed:" >&2
  print_errors
  exit 1
fi

start_seconds="${SECONDS}"
attempt=0
last_report=""
while ! check_all_rails; do
  ((attempt += 1))
  printf -v report '%s; ' "${check_errors[@]}"
  if [[ "${report}" != "${last_report}" ]] || ((attempt % 30 == 1)); then
    printf 'Waiting for four CX-7/RoCE rails (attempt %d): %s\n' \
      "${attempt}" "${report%; }" >&2
    last_report="${report}"
  fi

  if ((timeout_seconds > 0 && SECONDS - start_seconds >= timeout_seconds)); then
    printf 'Timed out after %d seconds waiting for CX-7/RoCE rails.\n' \
      "${timeout_seconds}" >&2
    exit 1
  fi
  /usr/bin/sleep "${poll_seconds}"
done

elapsed=$((SECONDS - start_seconds))
printf 'All four CX-7/RoCE rails are ready after %d seconds.\n' "${elapsed}"
