#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="/home/catid/venvs/vllm025/bin/python"
format="${1:?usage: $0 <fp8|nvfp4> <baseline|dflash> [draft_tokens]}"
mode="${2:?usage: $0 <fp8|nvfp4> <baseline|dflash> [draft_tokens]}"
draft_tokens="${3:-15}"
label="${format}-${mode}"
if [[ "${mode}" == "dflash" ]]; then
  label="${label}-k${draft_tokens}"
fi

case "${format}" in
  fp8)
    launcher="${root_dir}/bin/launch-fp8-pp2.sh"
    endpoints="http://127.0.0.1:8000"
    ;;
  nvfp4)
    launcher="${root_dir}/bin/launch-nvfp4-replicas.sh"
    endpoints="http://127.0.0.1:8000,http://192.168.100.11:8000"
    ;;
  *)
    echo "format must be fp8 or nvfp4" >&2
    exit 2
    ;;
esac

mkdir -p "${root_dir}/logs" "${root_dir}/results"
"${root_dir}/bin/stop-servers.sh"
sleep 2

cleanup() {
  "${root_dir}/bin/stop-servers.sh" || true
}
trap cleanup EXIT INT TERM

DFLASH_TOKENS="${draft_tokens}" "${launcher}" "${mode}" \
  >"${root_dir}/logs/${label}-spark1.log" 2>&1 &
server_pid=$!
echo "launched ${label}, spark1 pid=${server_pid}"

ready=0
for _ in $(seq 1 720); do
  if curl -fsS --max-time 2 http://127.0.0.1:8000/health >/dev/null; then
    if [[ "${format}" != "nvfp4" ]] || \
       curl -fsS --max-time 2 http://192.168.100.11:8000/health >/dev/null; then
      ready=1
      break
    fi
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    echo "server exited before becoming ready" >&2
    tail -200 "${root_dir}/logs/${label}-spark1.log" >&2
    exit 1
  fi
  sleep 5
done
if [[ "${ready}" != "1" ]]; then
  echo "server did not become ready within 60 minutes" >&2
  exit 1
fi

"${python_bin}" "${root_dir}/bench/rdma_counters.py" \
  --save "${root_dir}/results/${label}-spark1-before.json" >/dev/null
ssh -i /home/catid/.ssh/id_ed25519_dgx_cluster \
  -o IdentitiesOnly=yes spark2 \
  "'${python_bin}' '${root_dir}/bench/rdma_counters.py'" \
  >"${root_dir}/results/${label}-spark2-before.json"

"${python_bin}" "${root_dir}/bench/bench_serving.py" \
  --endpoints "${endpoints}" \
  --label "${label}" \
  --output "${root_dir}/results/${label}.json"

"${python_bin}" "${root_dir}/bench/rdma_counters.py" \
  --save "${root_dir}/results/${label}-spark1-after.json" >/dev/null
ssh -i /home/catid/.ssh/id_ed25519_dgx_cluster \
  -o IdentitiesOnly=yes spark2 \
  "'${python_bin}' '${root_dir}/bench/rdma_counters.py'" \
  >"${root_dir}/results/${label}-spark2-after.json"

"${python_bin}" "${root_dir}/bench/rdma_counters.py" \
  --before "${root_dir}/results/${label}-spark1-before.json" \
  --after "${root_dir}/results/${label}-spark1-after.json" \
  --save "${root_dir}/results/${label}-spark1-network-delta.json" >/dev/null
"${python_bin}" "${root_dir}/bench/rdma_counters.py" \
  --before "${root_dir}/results/${label}-spark2-before.json" \
  --after "${root_dir}/results/${label}-spark2-after.json" \
  --save "${root_dir}/results/${label}-spark2-network-delta.json" >/dev/null

trap - EXIT INT TERM
cleanup
wait "${server_pid}" 2>/dev/null || true
