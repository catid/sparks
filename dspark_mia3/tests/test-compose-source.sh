#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
compose="${root}/compose.yml"
fabric_check="${root}/bin/check-fabric.sh"
sync_script="${root}/bin/sync.sh"

grep -q -- '--tensor-parallel-size "$${TP_SIZE}"' "${compose}"
grep -q -- '--pipeline-parallel-size "$${PP_SIZE}"' "${compose}"
grep -q -- '--distributed-executor-backend mp' "${compose}"
grep -q -- '--nnodes "$${NNODES}"' "${compose}"
grep -q -- 'if \[\[ "$${ENABLE_DSPARK}" == "1" \]\]' "${compose}"
grep -q -- 'method.*dspark' "${compose}"
grep -q -- 'NCCL_IB_SUBNET_AWARE_ROUTING' "${compose}"
grep -q -- 'NCCL_NET_PLUGIN' "${compose}"
grep -q -- 'VLLM_HOST_IP: "${VLLM_HOST_IP}"' "${compose}"
grep -q -- 'MASTER_ADDR: "${MASTER_ADDR}"' "${compose}"
grep -Fq 'MASTER_ADDR="${master_ip}"' "${root}/bin/node-compose.sh"
grep -Fq 'VLLM_HOST_IP="${host_ip}"' "${root}/bin/node-compose.sh"
grep -Fq 'config:0|down:*|ps:*)' "${root}/bin/node-compose.sh"
grep -Fq 'master_ip="192.0.2.10"' "${root}/bin/node-compose.sh"
grep -Fq 'MIA3_RENDER_LAUNCH_CONFIG=1' "${root}/bin/validate-static.sh"
grep -Fq 'local cleanup starts independently' "${root}/bin/stop.sh"
if grep -q -- 'NCCL_CROSS_NIC' "${compose}"; then
  echo "Ring trial must leave NCCL_CROSS_NIC at the NCCL default." >&2
  exit 1
fi
grep -q -- 'restart: "no"' "${compose}"
grep -q -- 'pull_policy: never' "${compose}"
# shellcheck disable=SC2016  # This is a required literal in the target script.
grep -q -- '--check-once --scope ring --c3-port-map "${CX7_C3_PORT_MAP}"' "${fabric_check}"
grep -q -- 'remote_readiness_helper=' "${sync_script}"
grep -q -- 'local_readiness_sha=' "${sync_script}"

if grep -Eq '10\.10\.84\.|^HEAD_MGMT_IP=|^RANK[12]_MGMT_IP=|^MASTER_ADDR=' \
    "${root}/mia3.env"; then
  echo "Tracked Mia3 profile must be hostname-authoritative." >&2
  exit 1
fi

if grep -Eq '(sk-(proj|ant|or)-|AIza[0-9A-Za-z_-]{20,}|xox[abprs]-|hf_[0-9A-Za-z]{20,})' "${compose}"; then
  echo "Credential-like value found in Compose source." >&2
  exit 1
fi

echo "compose source tests passed"
