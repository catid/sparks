#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
readiness="${repo_root}/bin/wait-cx7-ready.sh"

grep -Fq 'CX7_EXPECTED_SPEED_MBPS' "${readiness}"
grep -Fq 'speed=${speed} Mb/s, expected at least ${expected_speed} Mb/s' \
  "${readiness}"

yaml_layout() {
  /usr/bin/awk '
    /^    [A-Za-z0-9]+:$/ {
      interface = $1
      sub(/:$/, "", interface)
    }
    interface != "" && /addresses: \[[0-9.]+\/24\]/ {
      address = $0
      sub(/^.*\[/, "", address)
      sub(/\].*$/, "", address)
      print interface " " address
    }
  ' "$1"
}

assert_yaml_layout() {
  local role="$1"
  local expected="$2"
  local actual
  actual="$(yaml_layout "${repo_root}/netplan/${role}-40-cx7.yaml")"
  [[ "${actual}" == "${expected}" ]] || {
    printf 'Unexpected Netplan layout for %s\nexpected:\n%s\nactual:\n%s\n' \
      "${role}" "${expected}" "${actual}" >&2
    exit 1
  }
}

assert_describe() {
  local role="$1"
  local scope="$2"
  local expected="$3"
  local actual
  actual="$(CX7_NODE_ROLE="${role}" "${readiness}" --describe --scope "${scope}")"
  [[ "${actual}" == "${expected}" ]] || {
    printf 'Unexpected readiness layout for %s/%s\nexpected:\n%s\nactual:\n%s\n' \
      "${role}" "${scope}" "${expected}" "${actual}" >&2
    exit 1
  }
}

assert_yaml_layout cerberus1 $'enp1s0f0np0 192.168.2.1/24\nenP2p1s0f0np0 192.168.3.1/24\nenp1s0f1np1 192.168.0.1/24\nenP2p1s0f1np1 192.168.1.1/24'
assert_yaml_layout cerberus2 $'enp1s0f0np0 192.168.0.2/24\nenP2p1s0f0np0 192.168.1.2/24\nenp1s0f1np1 192.168.4.1/24\nenP2p1s0f1np1 192.168.5.1/24'
assert_yaml_layout cerberus3 $'enp1s0f0np0 192.168.2.2/24\nenP2p1s0f0np0 192.168.3.2/24\nenp1s0f1np1 192.168.4.2/24\nenP2p1s0f1np1 192.168.5.2/24'

crossed_c3_layout="$(yaml_layout "${repo_root}/netplan/cerberus3-40-cx7-p0-to-c2.yaml")"
[[ "${crossed_c3_layout}" == $'enp1s0f0np0 192.168.4.2/24\nenP2p1s0f0np0 192.168.5.2/24\nenp1s0f1np1 192.168.2.2/24\nenP2p1s0f1np1 192.168.3.2/24' ]] || {
  printf 'Unexpected Netplan layout for crossed cerberus3 profile\nactual:\n%s\n' \
    "${crossed_c3_layout}" >&2
  exit 1
}

assert_describe cerberus1 ring $'node_role=cerberus1 scope=ring logical_links=4 expected_mtu=9000\nenp1s0f0np0 edge=cerberus1-cerberus3 peer_node=cerberus3 local=192.168.2.1/24 peer=192.168.2.2\nenP2p1s0f0np0 edge=cerberus1-cerberus3 peer_node=cerberus3 local=192.168.3.1/24 peer=192.168.3.2\nenp1s0f1np1 edge=cerberus1-cerberus2 peer_node=cerberus2 local=192.168.0.1/24 peer=192.168.0.2\nenP2p1s0f1np1 edge=cerberus1-cerberus2 peer_node=cerberus2 local=192.168.1.1/24 peer=192.168.1.2'
assert_describe cerberus2 ring $'node_role=cerberus2 scope=ring logical_links=4 expected_mtu=9000\nenp1s0f0np0 edge=cerberus1-cerberus2 peer_node=cerberus1 local=192.168.0.2/24 peer=192.168.0.1\nenP2p1s0f0np0 edge=cerberus1-cerberus2 peer_node=cerberus1 local=192.168.1.2/24 peer=192.168.1.1\nenp1s0f1np1 edge=cerberus2-cerberus3 peer_node=cerberus3 local=192.168.4.1/24 peer=192.168.4.2\nenP2p1s0f1np1 edge=cerberus2-cerberus3 peer_node=cerberus3 local=192.168.5.1/24 peer=192.168.5.2'
assert_describe cerberus3 ring $'node_role=cerberus3 scope=ring logical_links=4 expected_mtu=9000 c3_port_map=c3-p0-to-c1\nenp1s0f0np0 edge=cerberus1-cerberus3 peer_node=cerberus1 local=192.168.2.2/24 peer=192.168.2.1\nenP2p1s0f0np0 edge=cerberus1-cerberus3 peer_node=cerberus1 local=192.168.3.2/24 peer=192.168.3.1\nenp1s0f1np1 edge=cerberus2-cerberus3 peer_node=cerberus2 local=192.168.4.2/24 peer=192.168.4.1\nenP2p1s0f1np1 edge=cerberus2-cerberus3 peer_node=cerberus2 local=192.168.5.2/24 peer=192.168.5.1'

crossed_describe="$(
  CX7_NODE_ROLE=cerberus3 "${readiness}" --describe --scope ring \
    --c3-port-map c3-p0-to-c2
)"
[[ "${crossed_describe}" == $'node_role=cerberus3 scope=ring logical_links=4 expected_mtu=9000 c3_port_map=c3-p0-to-c2\nenp1s0f0np0 edge=cerberus2-cerberus3 peer_node=cerberus2 local=192.168.4.2/24 peer=192.168.4.1\nenP2p1s0f0np0 edge=cerberus2-cerberus3 peer_node=cerberus2 local=192.168.5.2/24 peer=192.168.5.1\nenp1s0f1np1 edge=cerberus1-cerberus3 peer_node=cerberus1 local=192.168.2.2/24 peer=192.168.2.1\nenP2p1s0f1np1 edge=cerberus1-cerberus3 peer_node=cerberus1 local=192.168.3.2/24 peer=192.168.3.1' ]] || {
  printf 'Unexpected readiness layout for crossed cerberus3 profile\nactual:\n%s\n' \
    "${crossed_describe}" >&2
  exit 1
}
[[ "${crossed_describe}" == "$(
  CX7_NODE_ROLE=cerberus3 CX7_C3_PORT_MAP=c3-p0-to-c2 \
    "${readiness}" --describe --scope ring
)" ]]

assert_describe cerberus1 tp2 $'node_role=cerberus1 scope=tp2 logical_links=2 expected_mtu=9000\nenp1s0f1np1 edge=cerberus1-cerberus2 peer_node=cerberus2 local=192.168.0.1/24 peer=192.168.0.2\nenP2p1s0f1np1 edge=cerberus1-cerberus2 peer_node=cerberus2 local=192.168.1.1/24 peer=192.168.1.2'
assert_describe cerberus2 tp2 $'node_role=cerberus2 scope=tp2 logical_links=2 expected_mtu=9000\nenp1s0f0np0 edge=cerberus1-cerberus2 peer_node=cerberus1 local=192.168.0.2/24 peer=192.168.0.1\nenP2p1s0f0np0 edge=cerberus1-cerberus2 peer_node=cerberus1 local=192.168.1.2/24 peer=192.168.1.1'

for suffix in 1 2 3; do
  canonical="$(CX7_NODE_ROLE="cerberus${suffix}" "${readiness}" --describe)"
  misspelled="$(CX7_NODE_ROLE="cerebrus${suffix}" "${readiness}" --describe)"
  transitional="$(CX7_NODE_ROLE="spark${suffix}" "${readiness}" --describe)"
  [[ "${canonical}" == "${misspelled}" ]]
  [[ "${canonical}" == "${transitional}" ]]
done

set +e
c3_tp2_output="$(CX7_NODE_ROLE=cerberus3 "${readiness}" --describe --scope tp2 2>&1)"
c3_tp2_status=$?
set -e
[[ "${c3_tp2_status}" == "2" ]]
/usr/bin/grep -Fq 'tp2 scope exists only on cerberus1 and cerberus2' <<<"${c3_tp2_output}"

set +e
invalid_map_output="$(
  CX7_NODE_ROLE=cerberus3 "${readiness}" --describe \
    --c3-port-map automatic 2>&1
)"
invalid_map_status=$?
set -e
[[ "${invalid_map_status}" == "2" ]]
/usr/bin/grep -Fq 'must be c3-p0-to-c1 or c3-p0-to-c2' <<<"${invalid_map_output}"

model_sync_output="$(
  MIA_ENV_FILE=mia-throughput.env \
    "${repo_root}/scripts/sync-pinned-model-multirail.sh" describe
)"
/usr/bin/grep -Fq 'destination=' <<<"${model_sync_output}"
/usr/bin/grep -Fq '@cerberus2:' <<<"${model_sync_output}"
/usr/bin/grep -Fq 'rails=192.168.0.2 192.168.1.2' <<<"${model_sync_output}"

# Production TP2 remains pinned to the direct Cerberus node 1 <-> node 2 edge
# and must never acquire a dependency on the C3 ring selector.
/usr/bin/grep -Fq -- '--check-once --scope tp2' \
  "${repo_root}/dspark_mia/bin/preflight.sh"
/usr/bin/grep -Fq -- '--wait --scope tp2' \
  "${repo_root}/systemd/dgx-spark-cx7-ready.service.in"
if /usr/bin/grep -Eq 'CX7_C3_PORT_MAP|--c3-port-map' \
  "${repo_root}/dspark_mia/bin/preflight.sh" \
  "${repo_root}/systemd/dgx-spark-cx7-ready.service.in"; then
  echo "Production TP2 readiness must not depend on the C3 port map." >&2
  exit 1
fi

echo "CX-7 ring-layout test passed: both explicit C3 maps, aliases, and TP2 isolation are deterministic."
