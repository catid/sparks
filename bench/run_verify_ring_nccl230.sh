#!/usr/bin/env bash
# Standalone three-node ring proof. It creates/removes only its own labelled
# containers and refuses to run while a model or any other GPU compute job is active.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
# shellcheck disable=SC1091
source "${repo_root}/dspark_mia3/bin/common.sh"

readonly expected_nccl_runtime=23007
# MASTER_ADDR is loaded by the sourced trial profile.
# shellcheck disable=SC2153
readonly master_addr="${MASTER_ADDR}"
readonly c3_port_map=c3-p0-to-c2
readonly nccl_ld_library_path="${NCCL_RUNTIME_PATH}:/usr/local/nvidia/lib64:/usr/local/cuda/lib64:/usr/local/nvidia/lib"
master_port="${RING_NCCL_MASTER_PORT:-29533}"
tensor_mib="${RING_NCCL_TENSOR_MIB:-512}"
warmups="${RING_NCCL_WARMUPS:-2}"
iterations="${RING_NCCL_ITERATIONS:-20}"
wait_seconds="${RING_NCCL_WAIT_SECONDS:-900}"
run_id="${RING_NCCL_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
artifact_root="${repo_root}/logs"
artifact_dir="${artifact_root}/nccl-ring-${run_id}"
verify_script="${repo_root}/bench/verify_ring_nccl.py"
counter_script="${repo_root}/bench/ring_rdma_counters.py"
artifact_validator="${repo_root}/bench/validate_ring_nccl_artifacts.py"
readiness_script="${repo_root}/bin/wait-cx7-ready.sh"
remote_checkout_root="$(dirname -- "${REMOTE_INSTALL_DIR}")"
image="$(awk -F= '$1 == "image" {sub(/^image=/, ""); print}' "${MIA3_UPSTREAM_LOCK}")"

for setting in "${master_port}" "${tensor_mib}" "${warmups}" "${iterations}" "${wait_seconds}"; do
  [[ "${setting}" =~ ^[1-9][0-9]*$ ]] || {
    echo "Ring NCCL numeric settings must be positive integers." >&2
    exit 2
  }
done
((master_port <= 65535 && tensor_mib <= 2048 && warmups <= 20 && iterations <= 100 && wait_seconds <= 3600)) || {
  echo "Ring NCCL setting exceeds its safety ceiling." >&2
  exit 2
}
[[ "${run_id}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$ ]] || {
  echo "RING_NCCL_RUN_ID contains unsafe characters or is too long." >&2
  exit 2
}
[[ "${image}" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] || {
  echo "UPSTREAM.lock does not contain an immutable container digest." >&2
  exit 2
}
for file in "${verify_script}" "${counter_script}" "${artifact_validator}" "${readiness_script}"; do
  [[ -f "${file}" && ! -L "${file}" ]] || {
    echo "Missing regular verifier input: ${file}" >&2
    exit 2
  }
done

need_command docker
need_command python3
need_command ssh
need_command timeout
require_head_host
require_ssh_identity

rank_hca() {
  # All nodes expose the same four names, but each value is resolved per rank
  # so a future hardware naming difference cannot silently inherit rank 0.
  case "$1" in
    0) printf '%s\n' '=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1' ;;
    1) printf '%s\n' '=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1' ;;
    2) printf '%s\n' '=rocep1s0f0,rocep1s0f1,roceP2p1s0f0,roceP2p1s0f1' ;;
    *) echo "Rank must be 0, 1, or 2." >&2; return 2 ;;
  esac
}

node_command() {
  local rank="$1"
  shift
  if [[ "${rank}" == 0 ]]; then
    "$@"
  else
    ssh_command "$(rank_host "${rank}")" "$@"
  fi
}

rank_repo_file() {
  local rank="$1" relative="$2"
  case "${rank}" in
    0) printf '%s/%s\n' "${repo_root}" "${relative}" ;;
    1|2) printf '%s/%s\n' "${remote_checkout_root}" "${relative}" ;;
    *) echo "Rank must be 0, 1, or 2." >&2; return 2 ;;
  esac
}

container_name() {
  printf 'cerebrus-ring-nccl-%s-r%s\n' "${run_id}" "$1"
}

started_ranks=()
cleanup_containers() {
  local original_status="$?" rank name benchmark_label run_label
  trap - EXIT INT TERM
  set +e
  for rank in "${started_ranks[@]}"; do
    name="$(container_name "${rank}")"
    benchmark_label="$(node_command "${rank}" sudo -n docker inspect \
      --format '{{ index .Config.Labels "com.cerebrus.benchmark" }}' "${name}" 2>/dev/null)"
    run_label="$(node_command "${rank}" sudo -n docker inspect \
      --format '{{ index .Config.Labels "com.cerebrus.run" }}' "${name}" 2>/dev/null)"
    if [[ "${benchmark_label}" == ring-nccl-verify && "${run_label}" == "${run_id}" ]]; then
      if [[ -d "${artifact_dir}" && ! -f "${artifact_dir}/$(rank_host "${rank}").log" ]]; then
        node_command "${rank}" sudo -n docker logs --timestamps "${name}" \
          >"${artifact_dir}/$(rank_host "${rank}").log" 2>&1 || true
      fi
      node_command "${rank}" sudo -n docker rm -f "${name}" >/dev/null || \
        echo "Warning: could not remove benchmark container ${name}." >&2
    elif [[ -n "${benchmark_label}${run_label}" ]]; then
      echo "Refusing to remove ${name}: ownership labels do not match." >&2
    fi
  done
  exit "${original_status}"
}
trap cleanup_containers EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Refuse mixed code before executing any repository helper on a worker. The
# mounted verifier, counter reader, readiness gate, and image lock must match
# rank 0 byte-for-byte.
for relative in \
  bench/verify_ring_nccl.py bench/ring_rdma_counters.py \
  bench/validate_ring_nccl_artifacts.py bin/wait-cx7-ready.sh \
  dspark_mia3/UPSTREAM.lock; do
  local_sha="$(sha256sum "${repo_root}/${relative}" | awk '{print $1}')"
  for rank in 1 2; do
    remote_sha="$(node_command "${rank}" sha256sum "$(rank_repo_file "${rank}" "${relative}")" | awk '{print $1}')"
    [[ "${remote_sha}" == "${local_sha}" ]] || {
      echo "$(rank_host "${rank}"): ${relative} differs from ${HEAD_HOST}." >&2
      exit 1
    }
  done
done

echo "Preflighting the three-node ring; no container has been started."
for rank in 0 1 2; do
  node="$(rank_host "${rank}")"
  actual_host="$(node_command "${rank}" hostname -s)"
  [[ "${actual_host}" == "${node}" ]] || {
    echo "Rank ${rank} expected ${node}, found ${actual_host}." >&2
    exit 1
  }
  node_command "${rank}" env "CX7_NODE_ROLE=${node}" \
    "$(rank_repo_file "${rank}" bin/wait-cx7-ready.sh)" --check-once --scope ring \
      --c3-port-map "${c3_port_map}"
  node_command "${rank}" sudo -n docker image inspect "${image}" >/dev/null || {
    echo "Pinned NCCL container image is missing on ${node}; this verifier never pulls." >&2
    exit 1
  }
  for rdma_device in rdma_cm uverbs0 uverbs1 uverbs2 uverbs3; do
    node_command "${rank}" test -c "/dev/infiniband/${rdma_device}" || {
      echo "${node}: missing RDMA character device ${rdma_device}." >&2
      exit 1
    }
  done
  workload_probe='if pgrep -af "[v]llm([.]entrypoints|[[:space:]]+(serve|entrypoints))" >/dev/null; then echo "vLLM is active on $(hostname -s); stop it explicitly before this capacity test." >&2; exit 1; fi; gpu_jobs="$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits)" || exit 1; if grep -Eq "^[[:space:]]*[0-9]+" <<<"${gpu_jobs}"; then echo "A GPU compute process is active on $(hostname -s): ${gpu_jobs}" >&2; exit 1; fi'
  node_command "${rank}" bash -c "${workload_probe}"
  active_image_containers="$(node_command "${rank}" sudo -n docker ps \
    --filter "ancestor=${image}" --format '{{.ID}} {{.Names}}')"
  if [[ -n "${active_image_containers}" ]]; then
    echo "The pinned GPU image is already running on ${node}: ${active_image_containers}" >&2
    exit 1
  fi
  port_probe='if ss -ltn "( sport = :$1 )" | tail -n +2 | grep -q .; then echo "Rendezvous port $1 is in use on $(hostname -s)." >&2; exit 1; fi'
  node_command "${rank}" bash -c "${port_probe}" -- "${master_port}"
  name="$(container_name "${rank}")"
  if node_command "${rank}" sudo -n docker container inspect "${name}" >/dev/null 2>&1; then
    echo "Benchmark container name already exists on ${node}: ${name}" >&2
    exit 1
  fi
done

umask 077
if [[ -L "${artifact_root}" || ( -e "${artifact_root}" && ! -d "${artifact_root}" ) ]]; then
  echo "Ring evidence root must be a real directory: ${artifact_root}" >&2
  exit 1
fi
if [[ ! -e "${artifact_root}" ]]; then
  mkdir -- "${artifact_root}"
  chmod 0700 -- "${artifact_root}"
fi
if [[ -e "${artifact_dir}" || -L "${artifact_dir}" ]]; then
  echo "Refusing to reuse ring evidence directory: ${artifact_dir}" >&2
  exit 1
fi
mkdir -- "${artifact_dir}"
chmod 0700 -- "${artifact_dir}"
for rank in 0 1 2; do
  node="$(rank_host "${rank}")"
  node_command "${rank}" python3 \
    "$(rank_repo_file "${rank}" bench/ring_rdma_counters.py)" \
    snapshot --node "${node}" \
    >"${artifact_dir}/${node}-before.json"
done

launch_rank() {
  local rank="$1" node name hca host_verify_script
  node="$(rank_host "${rank}")"
  name="$(container_name "${rank}")"
  hca="$(rank_hca "${rank}")"
  host_verify_script="$(rank_repo_file "${rank}" bench/verify_ring_nccl.py)"
  node_command "${rank}" sudo -n docker run -d \
    --name "${name}" \
    --label com.cerebrus.benchmark=ring-nccl-verify \
    --label "com.cerebrus.run=${run_id}" \
    --hostname "${node}" \
    --network host \
    --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,size=1g \
    --security-opt no-new-privileges \
    --cap-drop ALL \
    --gpus all \
    --device /dev/infiniband/rdma_cm \
    --device /dev/infiniband/uverbs0 \
    --device /dev/infiniband/uverbs1 \
    --device /dev/infiniband/uverbs2 \
    --device /dev/infiniband/uverbs3 \
    --ulimit memlock=-1:-1 \
    --volume "${host_verify_script}:/opt/cerebrus/verify_ring_nccl.py:ro" \
    --env "RANK=${rank}" \
    --env WORLD_SIZE=3 \
    --env "LOCAL_RANK=0" \
    --env "MASTER_ADDR=${master_addr}" \
    --env "MASTER_PORT=${master_port}" \
    --env "RING_NCCL_NODE=${node}" \
    --env "RING_NCCL_TENSOR_MIB=${tensor_mib}" \
    --env "RING_NCCL_WARMUPS=${warmups}" \
    --env "RING_NCCL_ITERATIONS=${iterations}" \
    --env HOME=/tmp \
    --env PYTHONPYCACHEPREFIX=/tmp/pycache \
    --env "LD_LIBRARY_PATH=${nccl_ld_library_path}" \
    --env NCCL_NET=IB \
    --env NCCL_IB_DISABLE=0 \
    --env "NCCL_IB_HCA=${hca}" \
    --env NCCL_IB_SUBNET_AWARE_ROUTING=1 \
    --env NCCL_NET_PLUGIN=none \
    --env NCCL_IB_MERGE_NICS=0 \
    --env NCCL_SOCKET_IFNAME==enP7s7 \
    --env NCCL_SOCKET_FAMILY=AF_INET \
    --env NCCL_IB_ADDR_FAMILY=AF_INET \
    --env NCCL_IB_ROCE_VERSION_NUM=2 \
    --env NCCL_DMABUF_ENABLE=1 \
    --env NCCL_NET_GDR_C2C=1 \
    --env NCCL_IB_QPS_PER_CONNECTION=1 \
    --env NCCL_IB_SPLIT_DATA_ON_QPS=0 \
    --env NCCL_CUMEM_ENABLE=0 \
    --env NCCL_NVLS_ENABLE=0 \
    --env NCCL_DEBUG=INFO \
    --env NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH \
    --env TORCH_NCCL_ASYNC_ERROR_HANDLING=1 \
    --entrypoint /usr/bin/python3 \
    "${image}" /opt/cerebrus/verify_ring_nccl.py
}

echo "Starting isolated NCCL ranks worker-first: rank 2, rank 1, rank 0."
for rank in 2 1 0; do
  started_ranks+=("${rank}")
  launch_rank "${rank}" >"${artifact_dir}/$(rank_host "${rank}")-container-id.txt"
done

declare -a wait_pids=()
for rank in 2 1 0; do
  (
    node_command "${rank}" timeout --kill-after=15 "${wait_seconds}" \
      sudo -n docker wait "$(container_name "${rank}")"
  ) >"${artifact_dir}/$(rank_host "${rank}")-exit-code.txt" &
  wait_pids[rank]="$!"
done

wait_failed=0
set +e
for rank in 2 1 0; do
  wait "${wait_pids[rank]}" || wait_failed=1
done
set -e

for rank in 0 1 2; do
  node="$(rank_host "${rank}")"
  node_command "${rank}" sudo -n docker logs --timestamps "$(container_name "${rank}")" \
    >"${artifact_dir}/${node}.log" 2>&1 || wait_failed=1
  node_command "${rank}" python3 \
    "$(rank_repo_file "${rank}" bench/ring_rdma_counters.py)" \
    snapshot --node "${node}" \
    >"${artifact_dir}/${node}-after.json"
  python3 "${counter_script}" diff \
    --before "${artifact_dir}/${node}-before.json" \
    --after "${artifact_dir}/${node}-after.json" \
    >"${artifact_dir}/${node}-diff.json"
  exit_code="$(tr -d '[:space:]' <"${artifact_dir}/${node}-exit-code.txt")"
  if [[ "${exit_code}" != 0 ]]; then
    echo "${node} NCCL container exit code is ${exit_code:-missing}." >&2
    wait_failed=1
  fi
done

python3 "${artifact_validator}" \
  --expected-runtime "${expected_nccl_runtime}" \
  --log "cerebrus1=${artifact_dir}/cerebrus1.log" \
  --log "cerebrus2=${artifact_dir}/cerebrus2.log" \
  --log "cerebrus3=${artifact_dir}/cerebrus3.log" \
  --diff "cerebrus1=${artifact_dir}/cerebrus1-diff.json" \
  --diff "cerebrus2=${artifact_dir}/cerebrus2-diff.json" \
  --diff "cerebrus3=${artifact_dir}/cerebrus3-diff.json" \
  >"${artifact_dir}/summary.json" || wait_failed=1

if ((wait_failed)); then
  echo "Ring NCCL verification failed; evidence retained in ${artifact_dir}." >&2
  exit 1
fi
echo "Ring NCCL 2.30 verification passed; evidence: ${artifact_dir}"
cat "${artifact_dir}/summary.json"
