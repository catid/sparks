#!/usr/bin/env bash
# Historical host-Python verifier. The current deployment obtains NCCL 2.30.7
# from the pinned DSpark container; this helper is only for installations that
# deliberately retain the earlier host venv plus locally built NCCL overlay.
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export USE_NCCL_230=1
source "${root_dir}/bin/cluster-env.sh"

nccl_library_dir="${NCCL_230_LIBRARY_DIR:-${HOME}/nccl/build/lib}"
if [[ ! -x "$(command -v torchrun || true)" ||
      ! -f "${nccl_library_dir}/libnccl.so.2" ]]; then
  echo "Legacy host NCCL verifier prerequisites are absent." >&2
  echo "Use serving-workload RDMA counters from docs/VALIDATION.md, or provide" >&2
  echo "VLLM_VENV_BIN, NCCL_230_PYTHON_OVERLAY, and NCCL_230_LIBRARY_DIR." >&2
  exit 2
fi

rank="${DGX_NODE_RANK:-0}"
if [[ "${rank}" != "0" && "${rank}" != "1" ]]; then
  echo "DGX_NODE_RANK must be 0 or 1." >&2
  exit 2
fi

export NCCL_IB_DISABLE=0
export NCCL_IB_HCA='=rocep1s0f0:1:0,roceP2p1s0f0:1:0,rocep1s0f1:1:1,roceP2p1s0f1:1:1'
export NCCL_NETDEVS_POLICY=ALL
export NCCL_CROSS_NIC=0
export NCCL_IB_MERGE_NICS=0
export NCCL_SOCKET_IFNAME='=enp1s0f0np0'
export NCCL_SOCKET_FAMILY=AF_INET
export GLOO_SOCKET_IFNAME=enp1s0f0np0
export NCCL_DMABUF_ENABLE=1
export NCCL_NET_GDR_C2C=1
export NCCL_IB_QPS_PER_CONNECTION="${NCCL_IB_QPS_PER_CONNECTION:-1}"
export NCCL_IB_SPLIT_DATA_ON_QPS="${NCCL_IB_SPLIT_DATA_ON_QPS:-0}"
export NCCL_DEBUG=INFO
export NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH
export NCCL_DEBUG_FILE="${root_dir}/logs/nccl230-multirail-qps${NCCL_IB_QPS_PER_CONNECTION}-rank${rank}.log"
export TORCH_NCCL_ASYNC_ERROR_HANDLING=1
unset NCCL_IB_GID_INDEX

show_counters() {
  local phase="$1"
  python3 - "${phase}" <<'PY'
import json
import os
import pathlib
import sys

result = {"rdma": {}, "errors": {}, "management": {}}
for device in pathlib.Path("/sys/class/infiniband").iterdir():
    counters = device / "ports/1/counters"
    result["rdma"][device.name] = {
        key: 4 * int((counters / key).read_text())
        for key in ("port_rcv_data", "port_xmit_data")
    }
    result["errors"][device.name] = {
        key: int((counters / key).read_text())
        for key in ("port_rcv_errors", "port_xmit_discards", "symbol_error")
    }
management_name = os.environ.get("SPARK_MANAGEMENT_INTERFACE", "")
if management_name:
    management = pathlib.Path("/sys/class/net") / management_name / "statistics"
    if management.is_dir():
        result["management"][management_name] = {
            key: int((management / key).read_text())
            for key in ("rx_bytes", "tx_bytes")
        }
print(f"NIC_COUNTERS_{sys.argv[1]}={json.dumps(result, sort_keys=True)}", flush=True)
PY
}

if [[ "${rank}" == "0" ]]; then
  ssh -i "${CLUSTER_SSH_KEY:-${HOME}/.ssh/id_ed25519_dgx_cluster}" \
    -o IdentityAgent=none \
    -o IdentitiesOnly=yes \
    -o BatchMode=yes \
    spark2 \
    "env DGX_NODE_RANK=1 VLLM_VENV_BIN='${VLLM_VENV_BIN:-${HOME}/venvs/vllm025/bin}' NCCL_230_PYTHON_OVERLAY='${NCCL_230_PYTHON_OVERLAY:-${HOME}/nccl-230-overlay}' NCCL_230_LIBRARY_DIR='${nccl_library_dir}' NCCL_IB_QPS_PER_CONNECTION='${NCCL_IB_QPS_PER_CONNECTION}' NCCL_IB_SPLIT_DATA_ON_QPS='${NCCL_IB_SPLIT_DATA_ON_QPS}' '${root_dir}/bench/run_verify_multirail_nccl230.sh'" \
    >"${root_dir}/logs/nccl230-multirail-spark2.stdout.log" 2>&1 &
  remote_pid=$!
fi

show_counters before
torchrun \
  --nnodes 2 \
  --nproc-per-node 1 \
  --node-rank "${rank}" \
  --master-addr 192.168.100.10 \
  --master-port 29519 \
  "${root_dir}/bench/verify_torch_nccl.py"
show_counters after

if [[ "${rank}" == "0" ]]; then
  wait "${remote_pid}"
  cat "${root_dir}/logs/nccl230-multirail-spark2.stdout.log"
fi
