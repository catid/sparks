#!/usr/bin/env bash

# Common runtime settings for the two directly connected DGX Sparks.
spark_home="${SPARK_HOME:-${HOME}}"
vllm_venv_bin="${VLLM_VENV_BIN:-${spark_home}/venvs/vllm025/bin}"
export PATH="${vllm_venv_bin}:/usr/local/cuda/bin:${PATH}"
export CUDA_HOME="/usr/local/cuda"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export HF_HOME="${HF_HOME:-${spark_home}/.cache/huggingface}"

# GB10 is compute capability 12.1. CUTE kernels need the architecture-specific
# target; cap cold-cache compilation parallelism to stay clear of unified-memory
# pressure while the 120B model is loading.
export CUTE_DSL_ARCH="sm_121a"
export MAX_JOBS="${MAX_JOBS:-4}"
export PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True"
export VLLM_BLOCKSCALE_FP8_GEMM_FLASHINFER="0"

# Use all four RoCE functions exposed by the two ConnectX-7 cables. Gloo and
# control traffic use the first direct link rather than the 10 GbE LAN.
export NCCL_IB_DISABLE="0"
export NCCL_IB_HCA="rocep1s0f0,roceP2p1s0f0,rocep1s0f1,roceP2p1s0f1"
export NCCL_SOCKET_IFNAME="enp1s0f0np0"
export GLOO_SOCKET_IFNAME="enp1s0f0np0"
export NCCL_DEBUG="${NCCL_DEBUG:-WARN}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-16}"

# Opt-in A/B path for the locally built NCCL 2.30.7. PyTorch's CUDA-13 wheel
# eagerly discovers CUDA libraries below each sys.path entry; PYTHONPATH makes
# it discover the overlay before the wheel's bundled NCCL 2.28.9. LD_PRELOAD
# alone is insufficient because PyTorch also opens its package-local NCCL.
if [[ "${USE_NCCL_230:-0}" == "1" ]]; then
  nccl_python_overlay="${NCCL_230_PYTHON_OVERLAY:-${spark_home}/nccl-230-overlay}"
  nccl_library_dir="${NCCL_230_LIBRARY_DIR:-${spark_home}/nccl/build/lib}"
  export PYTHONPATH="${nccl_python_overlay}${PYTHONPATH:+:${PYTHONPATH}}"
  export LD_LIBRARY_PATH="${nccl_library_dir}:${LD_LIBRARY_PATH}"
  export LD_PRELOAD="${nccl_library_dir}/libnccl.so.2${LD_PRELOAD:+:${LD_PRELOAD}}"
fi
