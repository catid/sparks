# Container and checkpoint provenance

The serving deployment is deliberately reproducible: source, container, and
model are pinned independently, the model is mounted read-only, and service
startup never downloads anything. Do not replace a digest or revision with a
floating tag when reproducing the setup.

## Locked artifacts

| Artifact | Lock |
| --- | --- |
| Recipe | `MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark` |
| Recipe commit | `0220360b752349c9b3129d64799246a4ec106640` |
| Recipe tree | `3869c6a7720746199122f7530b4692f696619f82` |
| Runtime image | `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8` |
| Active served model | `apetersson/DeepSeek-V4-Flash-0731-Abliterated-FP8` |
| Active revision | `7d02640c72a2c8127f116d3d1933ddfec5e4c0fa` |
| Reference/original model | `deepseek-ai/DeepSeek-V4-Flash-DSpark` |
| Reference revision | `62af8fffb2f7030cac4de2f0169f5b8d1101b646` |

The authoritative machine-readable records are
[`UPSTREAM.lock`](../dspark_mia/UPSTREAM.lock), the reference
[`MODEL.lock.json`](../dspark_mia/MODEL.lock.json), and the active
[`MODEL.abliterated-fp8.lock.json`](../dspark_mia/MODEL.abliterated-fp8.lock.json).
The selected environment names its model lock explicitly. The upstream recipe
is a Git submodule at [`dspark_mia/upstream`](../dspark_mia/upstream); local
orchestration changes live outside that submodule.

The model lock additionally verifies:

- 48 Safetensors shards;
- 166,886,535,336 total Safetensors bytes;
- the hashes and sizes of `config.json` and
  `model.safetensors.index.json`;
- Hugging Face download metadata for the exact revision; and
- the expected DeepSeek V4, DSpark block-size, and one-million-token model
  configuration.

The observed image stack used for the recorded baseline was:

| Component | Version |
| --- | --- |
| Architecture | `linux/arm64` |
| PyTorch | `2.11.0+cu130` |
| vLLM | `0.25.2.dev0+g752a3a504.d20260714` |
| Transformers | `5.13.1` |
| FlashInfer | `0.6.15` |
| NCCL distribution loaded by vLLM | `nvidia-nccl-cu13 2.30.7` |

`torch.cuda.nccl.version()` reports the NCCL version against which PyTorch was
built, 2.28.9 in this image. It is not proof of the shared object used by the
running process. The vLLM processes on both ranks map the identical
`nvidia/nccl/lib/libnccl.so.2` from the Python distribution, and
`ncclGetVersion()` returns 23007. In other words, the serving runtime is NCCL
2.30.7 even though PyTorch's build metadata says 2.28.9. See
[VLLM_TUNING.md](VLLM_TUNING.md#nccl-on-the-production-ring-edge) for transport
details.

## First-time artifact provisioning

Run lifecycle commands on C1. Both rank machines should have this repository
at the same absolute path, Docker available through `sudo -n docker`, the
direct TP2 edge configured, and key-based SSH from C1 to C2.
Follow the host and network setup first.

Initialize the pinned upstream checkout on both rank machines:

```bash
git submodule update --init --recursive
ssh cerebrus2 'cd /path/to/sparks && git submodule update --init --recursive'
```

Generate a local serving profile rather than editing a tracked profile. The
profile must end in `.env` and live directly under `dspark_mia/`:

```bash
bash ./scripts/configure-dspark-profile.sh --model active
export MIA_ENV_FILE=mia-throughput.local.env
```

Use `--profile agent --model active` and select `mia-agent.local.env` for the
C8 OpenClaw-oriented scheduler. The model selector atomically renders the
active repository, revision, host/container paths, and lock. Use
`--model official` only for the original DeepSeek reference checkpoint. Both
scheduler profiles retain the selected checkpoint,
image digest, API port, and served model IDs; their Compose project,
rendezvous port, tmp path, and scheduler limits differ.

Review the generated values before continuing, especially
`WORKER_INSTALL_DIR`, `CLUSTER_SSH_KEY`, both fabric addresses, model paths,
API/rendezvous ports, and the served model name. The selector is a basename
when used by the DSpark lifecycle wrappers; they resolve it inside
`dspark_mia/`.

Validate the rendered Compose configuration, then synchronize the selected
integration and profile to C2:

```bash
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/validate-static.sh
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/sync-worker.sh
```

Pull the exact image on all three ring nodes. The helper uses the selected
profile's dedicated SSH identity, refuses an unpinned image, verifies the
locked repository digest, requires identical image IDs on all three nodes, and
does not run a container:

```bash
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  ./scripts/pull-dspark-container.sh --pull-all
```

Authenticate the Hugging Face CLI using its credential store, never by adding
a token to this repository. Download the exact revision to C1:

```bash
hf auth login
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  ./scripts/download-pinned-model.sh --download
```

The download helper validates the checkpoint immediately. Copy it to C2
over the two logical links on the direct C1-P1↔C2-P0 edge and validate both
copies:

```bash
MIA_ENV_FILE="${MIA_ENV_FILE}" \
  ./scripts/sync-pinned-model-multirail.sh --sync
MIA_ENV_FILE="${MIA_ENV_FILE}" ./dspark_mia/bin/preflight.sh
```

`preflight.sh` is non-downloading and does not start a service. It requires the
ports to be free and rejects any already-running vLLM workload, so use the
live health checks in [OPERATIONS.md](OPERATIONS.md) after deployment instead
of running preflight against a serving cluster.

Production TP2 does not need model weights on C3. To retain the isolated PP3
compatibility/reproduction harness, render its portable profile and use
`dspark_mia3/bin/sync-model.sh 2` as documented in
[SETUP.md](SETUP.md#10-download-and-copy-the-exact-checkpoint). The separate
ring NCCL verifier needs the pinned image but never mounts the checkpoint.

## Runtime isolation

The local Compose override enforces:

- the digest-pinned image with `pull_policy: never`;
- `restart: "no"` on each individual rank;
- an exact, read-only model bind mount;
- `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1`;
- host networking for the distributed rendezvous and rank-0 API;
- one process/rank per Spark; and
- rank-specific NCCL HCA selectors for the direct C1-C2 edge.

Per-container restart is intentionally disabled. A lone tensor-parallel rank
cannot safely restart and join the other rank's existing NCCL generation.
C1's supervisor replaces both ranks as one generation when either rank
changes or fails.

The override requests a soft and hard `nofile` limit of 500,000. The active C8
generation was recreated with this setting and both ranks reported 500,000
through Docker inspection and process-visible `ulimit`. Existing containers
retain the limits with which they were created, so verify after later reloads:

```bash
container="$(
  sudo docker ps -q \
    --filter label=com.docker.compose.project=mia-dspark-agent \
    --filter label=com.docker.compose.service=vllm-dspark
)"
sudo docker inspect --format '{{json .HostConfig.Ulimits}}' "${container}"
sudo docker exec "${container}" sh -lc 'ulimit -Sn; ulimit -Hn'
```

Resolve the container through both Compose labels as shown. Do not select a
container by a broad image or process match.

## Rebuilding instead of pulling

The tested path uses the locked prebuilt image. If the registry artifact is
unavailable or a runtime patch must be developed, the pinned upstream
submodule includes the Dockerfiles, build scripts, and source-overlay
verification used to construct it. Start with:

```bash
sed -n '1,240p' dspark_mia/upstream/build-dspark-vllm-runtime.sh
sed -n '1,240p' dspark_mia/upstream/docs/SETUP.md
bash dspark_mia/upstream/scripts/verify-overlay-sources.sh
```

The locked upstream stores its shell files with mode 0644. Invoke standalone
helpers with `bash`; do not assume `./script.sh` is executable. The upstream
builder itself assumes executable helper bits, so make those mode changes only
in a disposable build copy rather than dirtying the pinned submodule used by
serving validation.

A locally rebuilt image is a new artifact. Give it its own immutable digest,
record the full toolchain and source commit, update the locks deliberately,
and rerun static, model, network, functional, and performance validation.
Results from an unrecorded local image are not comparable with this baseline.
