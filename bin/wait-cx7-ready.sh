#!/usr/bin/env bash
set -euo pipefail

# Validate the fixed three-node DGX Spark ring. Each physical CX-7 port exposes
# two logical Ethernet/RDMA links. Production TP2 intentionally checks only
# the direct cerberus1-P1 <-> cerberus2-P0 edge, so cerberus3 cannot block it.

readonly -a cx7_interfaces=(
  enp1s0f0np0
  enP2p1s0f0np0
  enp1s0f1np1
  enP2p1s0f1np1
)

expected_mtu="${CX7_EXPECTED_MTU:-9000}"
expected_speed="${CX7_EXPECTED_SPEED_MBPS:-200000}"
poll_seconds="${CX7_POLL_SECONDS:-2}"
ping_timeout="${CX7_PING_TIMEOUT_SECONDS:-1}"
timeout_seconds="${CX7_TIMEOUT_SECONDS:-0}"
requested_role="${CX7_NODE_ROLE:-$(/usr/bin/hostname -s)}"
scope="${CX7_SCOPE:-ring}"
c3_port_map="${CX7_C3_PORT_MAP:-c3-p0-to-c1}"
action="--wait"
action_seen=0

usage() {
  cat <<'EOF'
Usage: wait-cx7-ready.sh [--wait|--check-once|--describe] [--scope ring|tp2] \
  [--c3-port-map c3-p0-to-c1|c3-p0-to-c2]

Scopes:
  ring  Validate both local physical ports: four logical links to two peers.
  tp2   Validate only cerberus1-P1 <-> cerberus2-P0: two logical links.

Environment:
  CX7_NODE_ROLE             cerberus1, cerberus2, or cerberus3. Legacy
                            cerebrus1-3 and spark1-3 aliases are accepted.
                            The short hostname is used by default.
  CX7_SCOPE                 ring (default) or tp2; overridden by --scope.
  CX7_C3_PORT_MAP           c3-p0-to-c1 (current canonical default) or
                            c3-p0-to-c2 (NVIDIA crossed C3 cable layout).
                            Overridden by --c3-port-map. It changes only C3's
                            interface/address matrix and may be passed on all
                            nodes by a cluster-wide launcher.
  CX7_EXPECTED_MTU          Required MTU on every selected link (default: 9000).
  CX7_EXPECTED_SPEED_MBPS   Minimum negotiated link speed (default: 200000).
  CX7_POLL_SECONDS          Delay between checks (default: 2).
  CX7_PING_TIMEOUT_SECONDS  Per-link peer ping timeout (default: 1).
  CX7_TIMEOUT_SECONDS       Overall wait timeout; 0 waits forever (default: 0).
EOF
}

canonical_role() {
  case "$1" in
    cerberus1|cerebrus1|spark1) printf 'cerberus1\n' ;;
    cerberus2|cerebrus2|spark2) printf 'cerberus2\n' ;;
    cerberus3|cerebrus3|spark3) printf 'cerberus3\n' ;;
    *) return 1 ;;
  esac
}

require_uint() {
  local name="$1"
  local value="$2"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    printf '%s must be an unsigned integer (got %q)\n' "${name}" "${value}" >&2
    exit 2
  fi
}

while (($#)); do
  case "$1" in
    --wait|--check-once|--describe)
      if ((action_seen)); then
        echo "Choose only one readiness action." >&2
        exit 2
      fi
      action="$1"
      action_seen=1
      ;;
    --scope)
      shift
      if (($# == 0)); then
        echo "--scope requires ring or tp2." >&2
        exit 2
      fi
      scope="$1"
      ;;
    --scope=*) scope="${1#*=}" ;;
    --c3-port-map)
      shift
      if (($# == 0)); then
        echo "--c3-port-map requires c3-p0-to-c1 or c3-p0-to-c2." >&2
        exit 2
      fi
      c3_port_map="$1"
      ;;
    --c3-port-map=*) c3_port_map="${1#*=}" ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
  shift
done

case "${scope}" in
  ring|tp2) ;;
  *)
    echo "CX7 scope must be ring or tp2 (got ${scope})." >&2
    exit 2
    ;;
esac
case "${c3_port_map}" in
  c3-p0-to-c1|c3-p0-to-c2) ;;
  *)
    echo "CX7 C3 port map must be c3-p0-to-c1 or c3-p0-to-c2 (got ${c3_port_map})." >&2
    exit 2
    ;;
esac

require_uint CX7_EXPECTED_MTU "${expected_mtu}"
require_uint CX7_EXPECTED_SPEED_MBPS "${expected_speed}"
require_uint CX7_POLL_SECONDS "${poll_seconds}"
require_uint CX7_PING_TIMEOUT_SECONDS "${ping_timeout}"
require_uint CX7_TIMEOUT_SECONDS "${timeout_seconds}"
if ((expected_mtu < 576 || expected_speed < 1 || poll_seconds < 1 || ping_timeout < 1)); then
  echo "MTU must be at least 576, and speed/poll/ping values must be positive." >&2
  exit 2
fi

if ! node_role="$(canonical_role "${requested_role}")"; then
  echo "Cannot infer a ring role from ${requested_role}; set CX7_NODE_ROLE to cerberus1, cerberus2, or cerberus3." >&2
  exit 2
fi
if [[ "${scope}" == "tp2" && "${node_role}" == "cerberus3" ]]; then
  echo "The production tp2 scope exists only on cerberus1 and cerberus2." >&2
  exit 2
fi

declare -a local_cidrs=()
declare -a peer_ips=()
declare -a peer_nodes=()
declare -a edge_names=()

case "${node_role}" in
  cerberus1)
    # P0 <-> the selected cerberus3 port; P1 <-> cerberus2 P0.
    local_cidrs=(192.168.2.1/24 192.168.3.1/24 192.168.0.1/24 192.168.1.1/24)
    peer_ips=(192.168.2.2 192.168.3.2 192.168.0.2 192.168.1.2)
    peer_nodes=(cerberus3 cerberus3 cerberus2 cerberus2)
    edge_names=(cerberus1-cerberus3 cerberus1-cerberus3 cerberus1-cerberus2 cerberus1-cerberus2)
    ;;
  cerberus2)
    # P0 <-> cerberus1 P1; P1 <-> the selected cerberus3 port.
    local_cidrs=(192.168.0.2/24 192.168.1.2/24 192.168.4.1/24 192.168.5.1/24)
    peer_ips=(192.168.0.1 192.168.1.1 192.168.4.2 192.168.5.2)
    peer_nodes=(cerberus1 cerberus1 cerberus3 cerberus3)
    edge_names=(cerberus1-cerberus2 cerberus1-cerberus2 cerberus2-cerberus3 cerberus2-cerberus3)
    ;;
  cerberus3)
    case "${c3_port_map}" in
      c3-p0-to-c1)
        # Current canonical wiring: C3 P0 <-> C1 P0; C3 P1 <-> C2 P1.
        local_cidrs=(192.168.2.2/24 192.168.3.2/24 192.168.4.2/24 192.168.5.2/24)
        peer_ips=(192.168.2.1 192.168.3.1 192.168.4.1 192.168.5.1)
        peer_nodes=(cerberus1 cerberus1 cerberus2 cerberus2)
        edge_names=(cerberus1-cerberus3 cerberus1-cerberus3 cerberus2-cerberus3 cerberus2-cerberus3)
        ;;
      c3-p0-to-c2)
        # NVIDIA crossed wiring: C3 P0 <-> C2 P1; C3 P1 <-> C1 P0.
        local_cidrs=(192.168.4.2/24 192.168.5.2/24 192.168.2.2/24 192.168.3.2/24)
        peer_ips=(192.168.4.1 192.168.5.1 192.168.2.1 192.168.3.1)
        peer_nodes=(cerberus2 cerberus2 cerberus1 cerberus1)
        edge_names=(cerberus2-cerberus3 cerberus2-cerberus3 cerberus1-cerberus3 cerberus1-cerberus3)
        ;;
    esac
    ;;
esac

declare -a selected_indices=()
for index in "${!cx7_interfaces[@]}"; do
  if [[ "${scope}" == "ring" ||
        "${edge_names[index]}" == "cerberus1-cerberus2" ]]; then
    selected_indices+=("${index}")
  fi
done
readonly selected_count="${#selected_indices[@]}"

describe_layout() {
  local index
  printf 'node_role=%s scope=%s logical_links=%s expected_mtu=%s' \
    "${node_role}" "${scope}" "${selected_count}" "${expected_mtu}"
  if [[ "${node_role}" == "cerberus3" ]]; then
    printf ' c3_port_map=%s' "${c3_port_map}"
  fi
  printf '\n'
  for index in "${selected_indices[@]}"; do
    printf '%s edge=%s peer_node=%s local=%s peer=%s\n' \
      "${cx7_interfaces[index]}" "${edge_names[index]}" \
      "${peer_nodes[index]}" "${local_cidrs[index]}" "${peer_ips[index]}"
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

check_selected_links() {
  local rdma_links=""
  local index interface local_cidr peer_ip carrier mtu speed
  check_errors=()

  if ! rdma_links="$(/usr/bin/rdma link show 2>&1)"; then
    add_error "rdma link show failed: ${rdma_links}"
    rdma_links=""
  fi

  for index in "${selected_indices[@]}"; do
    interface="${cx7_interfaces[index]}"
    local_cidr="${local_cidrs[index]}"
    peer_ip="${peer_ips[index]}"

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

    if ! speed="$(<"/sys/class/net/${interface}/speed")"; then
      add_error "${interface}: cannot read negotiated speed"
      continue
    fi
    if [[ ! "${speed}" =~ ^[0-9]+$ ]] || ((10#${speed} < expected_speed)); then
      add_error "${interface}: speed=${speed} Mb/s, expected at least ${expected_speed} Mb/s"
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
      add_error "${interface}: peer ${peer_ip} (${peer_nodes[index]}) unreachable"
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
  if check_selected_links; then
    printf '%d required CX-7/RoCE logical links are ready (%s scope).\n' \
      "${selected_count}" "${scope}"
    exit 0
  fi
  printf 'CX-7/RoCE readiness check failed for %s (%s scope):\n' \
    "${node_role}" "${scope}" >&2
  print_errors
  exit 1
fi

start_seconds="${SECONDS}"
attempt=0
last_report=""
while ! check_selected_links; do
  ((attempt += 1))
  printf -v report '%s; ' "${check_errors[@]}"
  if [[ "${report}" != "${last_report}" ]] || ((attempt % 30 == 1)); then
    printf 'Waiting for %d CX-7/RoCE logical links (%s, attempt %d): %s\n' \
      "${selected_count}" "${scope}" "${attempt}" "${report%; }" >&2
    last_report="${report}"
  fi

  if ((timeout_seconds > 0 && SECONDS - start_seconds >= timeout_seconds)); then
    printf 'Timed out after %d seconds waiting for CX-7/RoCE links.\n' \
      "${timeout_seconds}" >&2
    exit 1
  fi
  /usr/bin/sleep "${poll_seconds}"
done

elapsed=$((SECONDS - start_seconds))
printf '%d required CX-7/RoCE logical links are ready after %d seconds (%s scope).\n' \
  "${selected_count}" "${elapsed}" "${scope}"
