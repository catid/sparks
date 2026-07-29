# Software inventory and pins

This is the reproducibility snapshot from both Sparks on 2026-07-29. Versions
are evidence of the working deployment, not an instruction to downgrade a
newer supported DGX OS automatically.

## Host stack

Both nodes matched unless noted:

| Component | Audited version |
| --- | --- |
| DGX OTA | 7.5.0 |
| Base DGX software build | 7.2.3 |
| Ubuntu | 24.04.4 LTS (Noble, arm64) |
| Kernel | `6.17.0-1029-nvidia` |
| GPU | NVIDIA GB10 |
| NVIDIA driver | `580.173.02` |
| CUDA toolkit / nvcc | 13.0 / 13.0.88 |
| Docker Engine | 29.2.1 |
| Docker Compose | v5.0.2 |
| Python | 3.12.3 |
| systemd | 255 |
| NetworkManager | 1.46.0 |
| Netplan | 1.1.2 |
| rdma-core | 50.0 |
| perftest | 24.01 |
| OpenSSH | 9.6p1 |
| ethtool | 6.7 |
| nvme-cli | 2.8 |
| Nginx, Spark 1 only | 1.24.0 |

Do not copy `/etc/dgx-release` into an issue or report: it includes the
machine's serial number. Record only its software-build and OTA fields.

The repository's package helper installs the small host toolset required for
validation and operations:

```bash
scripts/install-host-packages.sh --install
```

It intentionally does not replace the NVIDIA kernel, driver, CUDA toolkit, or
Docker runtime supplied by DGX OS.

## Pinned source, image, and model

`dspark_mia/UPSTREAM.lock` is the machine-readable provenance authority:

| Artifact | Pin |
| --- | --- |
| Recipe | `MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark` |
| Recipe commit | `0220360b752349c9b3129d64799246a4ec106640` |
| Container | `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8` |
| Model | `deepseek-ai/DeepSeek-V4-Flash-DSpark` |
| Model revision | `62af8fffb2f7030cac4de2f0169f5b8d1101b646` |

The upstream recipe is a Git submodule checked out detached at that commit;
local behavior is implemented by adjacent wrappers and the Compose override,
not by silently editing the submodule.

The audited arm64 image had image ID:

```text
sha256:3430d6614a8e2925f34d059af6caf05aff42387326db4d05639a60f10f2654d8
```

The model lock additionally requires 48 Safetensors shards totaling
166,886,535,336 bytes and hashes its checkpoint index and `config.json`.
Each Spark stores a complete local copy outside Git. Use
`scripts/download-pinned-model.sh`, `scripts/sync-pinned-model-multirail.sh`,
and `dspark_mia/bin/validate-model.sh`; lifecycle commands never download
weights.

## Container Python/CUDA stack

Both running ranks reported:

| Component | Version |
| --- | --- |
| PyTorch | `2.11.0+cu130` |
| vLLM | `0.25.2.dev0+g752a3a504.d20260714` |
| Transformers | `5.13.1` |
| FlashInfer | `0.6.15` |
| NCCL Python distribution | `nvidia-nccl-cu13` 2.30.7 |
| Architecture target | `sm_121a` / `12.1a` |

This is a purpose-built GB10 image. A generic vLLM or PyTorch container is not
an equivalent baseline even if its package version looks newer.

The active profile targets GB10 explicitly:

```bash
CUTE_DSL_ARCH=sm_121a
TORCH_CUDA_ARCH_LIST=12.1a
FLASHINFER_CUDA_ARCH_LIST=12.1a
FLASHINFER_DISABLE_VERSION_CHECK=1
VLLM_USE_FLASHINFER_SAMPLER=1
VLLM_USE_B12X_MOE=1
```

It also uses the image's FlashInfer B12X W4A16 MoE implementation and enables
FlashInfer autotuning. JIT caches belong outside the repository; expect a cold
environment to spend time compiling or autotuning before its first stable
measurement.

## NCCL: compile report versus loaded runtime

There are three different values that can easily be confused:

1. `torch.cuda.nccl.version()` reports PyTorch's compile-time tuple
   `(2, 28, 9)`.
2. The host/system NCCL library is also 2.28.9.
3. Every audited live vLLM process actually mapped the container wheel's
   `/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/libnccl.so.2`;
   `ncclGetVersion()` returned `23007`, meaning NCCL 2.30.7.

The two ranks mapped the same library hash. The wheel library wins because of
the container's `libtorch_cuda.so` runtime search path. Therefore, the PyTorch
tuple alone does **not** prove which NCCL services a running collective.

Verify after every image or PyTorch change:

```bash
container=NAME_OF_RUNNING_RANK_CONTAINER
pid=$(sudo docker inspect --format '{{.State.Pid}}' "$container")
sudo awk '/libnccl\.so/{print $6}' "/proc/$pid/maps" | sort -u

sudo docker exec -i "$container" python3 - <<'PY'
import ctypes
import torch

print("torch compile tuple:", torch.cuda.nccl.version())
path = "/usr/local/lib/python3.12/dist-packages/nvidia/nccl/lib/libnccl.so.2"
lib = ctypes.CDLL(path)
version = ctypes.c_int()
if lib.ncclGetVersion(ctypes.byref(version)) != 0:
    raise SystemExit("ncclGetVersion failed")
print("loaded wheel candidate:", version.value)
PY
```

Also inspect the rank's `/proc/.../maps`; explicitly loading a candidate in a
new Python process does not by itself prove that an existing vLLM process uses
it. `bench/verify_torch_nccl.py` and the multirail verifier provide further
collective checks.

## Upgrade discipline

Keep the working pins together. Before adopting a new nightly, image, kernel,
driver, or model revision:

1. record all current pins and the model lock validation;
2. use a new Compose project/profile and ports;
3. confirm both ranks load the same NCCL library;
4. prove four-rail RDMA counter deltas;
5. inspect vLLM startup for rejected or renamed flags;
6. repeat fixed 1/2/4/8/16/32-concurrency prompts;
7. retest long-context reasoning/tool output, not only token rate;
8. test coordinated rank and container recovery; and
9. update lock files only after the new configuration passes.

Do not benchmark two 120B services simultaneously: unified memory is already
committed to the TP2 deployment. Do not infer that a successful import proves
CUDA graphs, NVFP4 kernels, DFlash, or NCCL function under load.

## Credentials and caches

Hugging Face credentials are needed only to acquire restricted artifacts.
Authenticate through the CLI or a mode-0600 host credential file; never add a
token to a profile, command transcript, shell startup file, image layer, or
Git history.

The following are host-local and intentionally untracked:

- model trees and Hugging Face caches;
- Docker credentials, images, containers, and volumes;
- CUDA/FlashInfer/Triton/CuTeDSL JIT caches;
- SSH identities and agent sockets;
- dashboard and OpenClaw live configuration; and
- logs, request/response trajectories, telemetry, and supervisor state.
