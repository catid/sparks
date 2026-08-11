#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
launcher="${repo_root}/bench/run_verify_ring_nccl230.sh"

bash -n "${launcher}"
python3 - <<'PY' "${repo_root}"
import ast
from pathlib import Path
import sys

root = Path(sys.argv[1])
for relative in (
    "bench/verify_ring_nccl.py",
    "bench/ring_rdma_counters.py",
    "bench/validate_ring_nccl_artifacts.py",
    "bench/tests/test_ring_nccl.py",
):
    ast.parse((root / relative).read_text(), filename=relative)
PY

# The verifier must honor the selected portable trial profile rather than
# silently restoring the audited site's management address.
grep -Fq 'readonly master_addr="${MASTER_ADDR}"' "${launcher}"
grep -q 'readonly c3_port_map=c3-p0-to-c2' "${launcher}"
grep -q 'expected_nccl_runtime=23007' "${launcher}"
grep -q 'NCCL_IB_SUBNET_AWARE_ROUTING=1' "${launcher}"
grep -q 'NCCL_NET_PLUGIN=none' "${launcher}"
grep -q 'NCCL_IB_MERGE_NICS=0' "${launcher}"
grep -q 'NCCL_SOCKET_IFNAME==enP7s7' "${launcher}"
grep -q 'com.cerebrus.benchmark=ring-nccl-verify' "${launcher}"
grep -q 'snapshot --node' "${launcher}"
grep -q -- '-before.json' "${launcher}"
grep -q -- '-after.json' "${launcher}"
grep -q 'docker logs --timestamps' "${launcher}"
grep -q 'worker-first: rank 2, rank 1, rank 0' "${launcher}"
grep -q 'docker image inspect' "${launcher}"
grep -q 'query-compute-apps=pid' "${launcher}"
grep -q 'docker ps.*' "${launcher}"
grep -q 'this verifier never pulls' "${launcher}"
grep -Fq 'remote_checkout_root="$(dirname -- "${REMOTE_INSTALL_DIR}")"' \
  "${launcher}"
grep -Fq 'rank_repo_file "${rank}" bench/verify_ring_nccl.py' "${launcher}"
grep -Fq 'rank_repo_file "${rank}" bench/ring_rdma_counters.py' "${launcher}"
grep -Fq 'rank_repo_file "${rank}" bin/wait-cx7-ready.sh' "${launcher}"
grep -Fq '[[ -e "${artifact_dir}" || -L "${artifact_dir}" ]]' "${launcher}"
grep -Fq 'mkdir -- "${artifact_dir}"' "${launcher}"
if grep -Fq 'mkdir -p -- "${artifact_dir}"' "${launcher}"; then
  echo "Ring verifier must never reuse an existing evidence directory." >&2
  exit 1
fi
# shellcheck disable=SC2016  # This is a required literal in the target script.
grep -q -- '--c3-port-map "${c3_port_map}"' "${launcher}"
grep -q -- '--env "LD_LIBRARY_PATH=${nccl_ld_library_path}"' "${launcher}"
grep -q -- 'NCCL_RUNTIME_PATH.*nvidia/lib64.*cuda/lib64' "${launcher}"

if grep -q -- '--env NCCL_CROSS_NIC' "${launcher}"; then
  echo "Ring verifier must leave NCCL_CROSS_NIC at NCCL's automatic default." >&2
  exit 1
fi
if grep -q -- '--env NCCL_NETDEVS_POLICY' "${launcher}"; then
  echo "Ring verifier must leave NCCL_NETDEVS_POLICY at NCCL's AUTO default." >&2
  exit 1
fi
if grep -Eq 'systemctl|docker compose|pkill|killall|dspark_mia/bin/(start|stop)' "${launcher}"; then
  echo "Ring verifier contains a forbidden model/service lifecycle command." >&2
  exit 1
fi
if grep -Eq '(sk-(proj|ant|or)-|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-|hf_[0-9A-Za-z]{20,})' \
  "${launcher}" "${repo_root}/bench/verify_ring_nccl.py" \
  "${repo_root}/bench/ring_rdma_counters.py" \
  "${repo_root}/bench/validate_ring_nccl_artifacts.py"; then
  echo "Credential-like content found in ring verifier." >&2
  exit 1
fi

PYTHONDONTWRITEBYTECODE=1 python3 "${repo_root}/bench/tests/test_ring_nccl.py"
echo "ring NCCL static tests passed"
