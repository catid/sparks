#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "${root_dir}/bin/cluster-env.sh"

rank="${DGX_NODE_RANK:-0}"
cerberus2_host="${CERBERUS2_SSH_HOST:-${SPARK2_SSH_HOST:-cerberus2.local}}"
[[ "${cerberus2_host}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "Unsafe Cerberus 2 SSH host: ${cerberus2_host}" >&2
  exit 2
}
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,NET

show_counters() {
  local phase="$1"
  python3 - "${phase}" <<'PY'
import json
import pathlib
import sys

names = [
    "enp1s0f0np0", "enP2p1s0f0np0", "enp1s0f1np1", "enP2p1s0f1np1",
    "enP7s7",
]
result = {"netdev": {}, "rdma": {}}
for name in names:
    base = pathlib.Path("/sys/class/net") / name / "statistics"
    result["netdev"][name] = {
        key: int((base / key).read_text())
        for key in ("rx_bytes", "tx_bytes")
    }
for device in pathlib.Path("/sys/class/infiniband").iterdir():
    base = device / "ports/1/counters"
    # InfiniBand/RoCE data counters are in units of four octets.
    result["rdma"][device.name] = {
        "rx_bytes": 4 * int((base / "port_rcv_data").read_text()),
        "tx_bytes": 4 * int((base / "port_xmit_data").read_text()),
    }
print(f"NIC_COUNTERS_{sys.argv[1]}={json.dumps(result, sort_keys=True)}", flush=True)
PY
}

if [[ "${rank}" == "0" ]]; then
  ssh -i "${CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}" \
    -o IdentityAgent=none \
    -o IdentitiesOnly=yes "${cerberus2_host}" \
    "env DGX_NODE_RANK=1 USE_NCCL_230='${USE_NCCL_230:-0}' '${root_dir}/bench/run_verify_network.sh'" \
    >"${root_dir}/logs/torch-nccl-cerberus2.log" 2>&1 &
  remote_pid=$!
fi

show_counters before
torchrun \
  --nnodes 2 \
  --nproc-per-node 1 \
  --node-rank "${rank}" \
  --master-addr 192.168.100.10 \
  --master-port 29509 \
  "${root_dir}/bench/verify_torch_nccl.py"
show_counters after

if [[ "${rank}" == "0" ]]; then
  wait "${remote_pid}"
  cat "${root_dir}/logs/torch-nccl-cerberus2.log"
fi
