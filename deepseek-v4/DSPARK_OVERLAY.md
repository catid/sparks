# DeepSeek V4 Flash DSpark checkpoint compatibility

## Result

The official three-stage DSpark draft can be merged *structurally* with the
existing NVIDIA NVFP4 target by replacing the target's ignored `mtp.0` shard
with the official `mtp.0`, `mtp.1`, and `mtp.2` shards. It is **not currently a
safe runnable combination** with the installed vLLM 0.25.1 quantization
dispatch.

The problem is not the model architecture or tensor names. The problem is that
the existing target's routed experts are NVIDIA ModelOpt NVFP4 while the
official DSpark draft experts are native MXFP4. The DSpark model constructor
uses the target's global quantization config for its three draft decoder
layers, so it allocates the wrong parameter format before loading the native
MXFP4 draft tensors.

Do not point a production service at the overlay until this has either:

1. been fixed in vLLM with per-draft-layer native-MXFP4 dispatch and tested
   against known logits, or
2. been replaced by the homogeneous official checkpoint and the tested Anemll
   native-MXFP4 runtime.

No active service, GPU process, source checkpoint, or full model shard was
changed while producing this analysis.

## Pinned official artifacts

Source repository:
[deepseek-ai/DeepSeek-V4-Flash-DSpark](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark),
revision `62af8fffb2f7030cac4de2f0169f5b8d1101b646`.

| Artifact | Bytes | SHA-256 | Contents |
|---|---:|---|---|
| `config.json` | 1,888 | `6c8f3d2d3b48707541b88f32f22ef3f0f8a6b57d8523281e2b8d3cdb0ae9a023` | DSpark architecture |
| `model.safetensors.index.json` | 5,602,871 | `98efab455cf08dfbbbaaba6f570e1bf10bf927d2b4c3c453a59c2f6f0e3be92b` | 48-shard index |
| `model-00046-of-00048.safetensors` | 3,610,455,184 | `14810f274692bb771c3970e8cba45846c4aa2213dcfb0025ffebe788d229e18d` | 1,568 `mtp.0` tensors |
| `model-00047-of-00048.safetensors` | 3,560,111,960 | `7a44164698d90648a35c030c5eb369256d2c469306bfbf2b1ae27f35b6e57889` | 1,565 `mtp.1` tensors |
| `model-00048-of-00048.safetensors` | 3,692,775,244 | `a0bbb24f36d2ef6107250088e0f020f93aec0677cd24be3e9e69589547a7656f` | 1,572 `mtp.2` tensors |

The three weight shards are exactly `10,863,342,388` bytes
(`10.117276002 GiB`). Metadata plus weights are exactly `10,868,947,147`
bytes (`10.122495840 GiB`).

The official config supplies:

```text
dspark_block_size       5
dspark_noise_token_id   128799
dspark_target_layer_ids [40, 41, 42]
dspark_markov_rank      256
swiglu_limit            10.0
```

All checked scalar/structural fields match the local target: 43 layers, hidden
size 4096, 256 routed experts, expert intermediate size 2048, FP4 expert
weights, attention geometry, HC geometry, indexer geometry, and vocabulary.
The one intentional list extension is `compress_ratios`: the local target has
44 entries and the official DSpark config has the identical 44-entry prefix
plus `[0, 0]`. The overlay preserves the local list after validating that the
official suffix contains only zeros. Installed vLLM forces compression ratio
1 whenever a constructed layer index is at or above `num_hidden_layers`, so
the two extra draft entries are not indexed by its current attention adapter.

## Exact quantization evidence

Safetensors headers were read with HTTP byte ranges, not full-shard
downloads. The same representative expert tensor has these layouts:

| Checkpoint section | Tensor | Safetensors dtype | Physical shape | Interpretation |
|---|---|---|---|---|
| Official DSpark `mtp.0/1/2` | `w1.weight` | `I8` | `[2048, 2048]` | two packed FP4 values per byte |
| Official DSpark `mtp.0/1/2` | `w1.scale` | `F8_E8M0` | `[2048, 128]` | native MXFP4, K=4096 / block 32 |
| Local ignored `mtp.0` | `w1.weight` | `I8` | `[2048, 2048]` | two packed FP4 values per byte |
| Local ignored `mtp.0` | `w1.scale` | `F8_E8M0` | `[2048, 128]` | same native MXFP4 layout |
| Local converted base expert | `w1.weight` | `U8` | `[2048, 2048]` | ModelOpt NVFP4 packed weight |
| Local converted base expert | `w1.weight_scale` | `F8_E4M3` | `[2048, 256]` | NVFP4 K=4096 / group 16 |
| Local converted base expert | `w1.weight_scale_2` | `F32` | scalar | ModelOpt global weight scale |
| Local converted base expert | `w1.input_scale` | `F32` | scalar | ModelOpt activation scale |

This is important terminology: the official draft expert format is MXFP4 with
32-value blocks, not NVFP4 group 16. The official config's
`weight_block_size=[128,128]` describes its FP8 dense-weight scheme; it does
not change the routed experts' MXFP4 block size.

The local conversion intentionally ignored `mtp.*`. Its source MTP shard has
1,575 native-MXFP4 tensors, while layers 0 through 42 have the ModelOpt NVFP4
fields above. This independently confirms that the official DSpark draft is
physically compatible with the local checkpoint's *unconverted MTP format*,
but not with the converted base-expert allocation.

The deterministic merged index would contain:

```text
local base tensors        133,660
official DSpark tensors     4,705
merged tensors            138,365
merged tensor bytes   175,535,844,088 (163.480494253 GiB)
```

The source's old MTP tensor payload is `3,593,787,756` bytes. The official
three-stage draft payload is `10,862,838,300` bytes, a payload increase of
`7,269,050,544` bytes. The base shards remain symlinks, so the actual new
artifact storage is the `10.122495840 GiB` download above.

## Why the local overlay does not yet load correctly

The installed stack is vLLM 0.25.1, PyTorch 2.11.0+cu130, and FlashInfer
0.6.15.dev20260712.

The exact vLLM dispatch is:

1. `DeepseekV4FP8Config.get_quant_method()` sees a routed-expert layer.
2. The local target config globally declares `expert_dtype=fp4` and
   `moe_quant_algo=NVFP4`.
3. Unless the *constructed layer prefix* matches the ignore list, vLLM selects
   `ModelOptNvFp4FusedMoE`; otherwise it selects native `Mxfp4MoEMethod`.
4. The target ignore list contains `mtp.*`, but DSpark constructs its three
   decoder layers with quantization prefixes `model.layers.43`,
   `model.layers.44`, and `model.layers.45`.
5. Consequently `mtp.*` does not match the prefixes used during allocation.
   The draft receives NVFP4 `uint8` weights, E4M3 group-16 scales, global
   scale-2 fields, and input-scale fields.
6. The DSpark loader later remaps checkpoint names from `mtp.N.*` to its draft
   module and renames `.scale` to `.weight_scale`, but this does not create the
   missing scale-2/input-scale tensors and cannot turn `[*, K/32]` E8M0
   scales into `[*, K/16]` E4M3 scales.

This conclusion is static and conservative: a live load was deliberately not
attempted because it would consume substantial unified memory and disturb the
active benchmark environment.

The required upstream fix is per-layer quantization selection for the DSpark
draft—for example, explicitly dispatch the three draft `RoutedExperts` through
`Mxfp4MoEMethod` while leaving the target layers on ModelOpt NVFP4—plus a
loader test that verifies all expected parameters are loaded and a logits
parity test.

## B12X and the SwiGLU clamp

The local `has_flashinfer_b12x_gemm()` and
`has_flashinfer_b12x_moe()` capability checks return true, but those checks are
not sufficient for correctness.

The stock local `flashinfer_b12x` MoE backend is NVFP4-only. vLLM 0.25.1
explicitly excludes it when `swiglu_limit` is set, and an explicit
`--moe-backend flashinfer_b12x` raises an error explaining that the backend
does not apply the clamp. The safe current NVFP4 choices remain
`flashinfer_cutlass` (the existing default) or `flashinfer_trtllm`.

The [Anemll `dspark-vllm-gx10`](https://github.com/Anemll/dspark-vllm-gx10)
runtime is different. Repository commit
`47503f8e38dadd4dededca798150db2619594fce` pins:

```text
vLLM       752a3a504485790a2e8491cacbb35c137339ad34 (v0.25.1 source)
FlashInfer 0472b9b3f2fba11b463f8526f390297d52a8aad7
b12x       7dc6fb8fcc6446ea093537d1657df81985fa5f43
```

Its overlay adds a separate native-MXFP4 `B12xExperts` adapter. That adapter
passes `quant_config.gemm1_clamp_limit` through scratch planning, binding, and
execution. The pinned b12x W4A16 kernel:

- validates that the limit is positive and used only with gated SiLU;
- clamps gate to at most the limit;
- clamps the up value into `[-limit, +limit]`; and
- has an eager and CUDA-graph test against an oracle with
  `swiglu_limit=10.0`.

Therefore the Anemll path is a credible, tested B12X route for the
*homogeneous official/native-MXFP4 checkpoint*. It does not solve the local
overlay's global NVFP4 allocation problem.

That alternative requires the full official checkpoint, not only the draft
overlay. Its 48 Safetensors files total exactly `166,886,535,336` bytes
(`155.425197758 GiB`), with `166,878,536,440` tensor-payload bytes in the
index. It can be downloaded once and synchronized over the CX-7 links, but
each node still needs an identical local checkpoint tree for the TP=2 launch.

The prebuilt tag `ghcr.io/anemll/dspark-vllm-gx10:0.1.1` is third-party and
the tag itself is mutable. A manifest-only inspection on 2026-07-29 resolved
the ARM64 image to:

```text
ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8
```

Use that digest for the controlled trial and re-inspect the tag before treating
it as equivalent. Alternatively, build from the exact repository commit and
dependency pins above. Do not mix its Python overlay with the local vLLM
environment piecemeal.

The companion
[MiaAI-Lab two-Spark recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
was inspected at commit
`2a869d6eeefda2a65b2fce0aad452c34d41d5630`. Its current Compose path uses
the Anemll 0.1.1 image, the full official DSpark checkpoint, TP=2, PP=1,
`nvfp4_ds_mla` KV cache, native-MXFP4 B12X MoE, and probabilistic DSpark
speculation. Its published throughput excludes prefill and the first output
token and uses at most 512 output tokens, so those numbers are not comparable
to this project's 1,024-input/1,024-output end-to-end matrix.

The installed vLLM also explicitly raises
`DSpark does not support pipeline parallelism`; use TP=2 for an official
DSpark trial. PP=2 remains relevant only to non-DSpark target/baseline tests.

## Safe overlay builder

[`build_dspark_nvfp4_overlay.py`](../bin/build_dspark_nvfp4_overlay.py) is
non-destructive and separates planning, downloading, building, and
validation. It pins exact bytes and SHA-256 digests, resumes through `.part`
files, quarantines bad partials, verifies Safetensors headers against the
index, uses distinct imported shard names, and atomically publishes a
symlink-based directory.

Read-only plan:

```bash
/home/catid/dgx-spark-laguna/bin/build_dspark_nvfp4_overlay.py plan
```

Fetch only the 5,604,759 bytes of pinned metadata:

```bash
/home/catid/dgx-spark-laguna/bin/build_dspark_nvfp4_overlay.py fetch-metadata
```

Only after deciding to develop the mixed-quant vLLM fix, fetch the three large
shards and build:

```bash
/home/catid/dgx-spark-laguna/bin/build_dspark_nvfp4_overlay.py fetch
/home/catid/dgx-spark-laguna/bin/build_dspark_nvfp4_overlay.py build
/home/catid/dgx-spark-laguna/bin/build_dspark_nvfp4_overlay.py validate
```

The default output is
`/home/catid/models/DeepSeek-V4-Flash-NVFP4-DSpark-overlay`. Its provenance
file deliberately records
`runtime_status=structurally_valid_runtime_experimental`.

After a local build is validated, synchronize the *pinned artifact directory,
overlay metadata, and symlinks' source paths* to Spark2 before any TP trial.
Do not copy only the overlay directory: its symlinks intentionally refer to
the base checkpoint and artifact directory. Run `validate` independently on
both nodes and compare the provenance and index SHA-256 values.

## Launcher controls

The existing two-node launcher keeps its current default but now permits
controlled backend experiments:

```text
DEEPSEEK_MOE_BACKEND     default: flashinfer_cutlass
DEEPSEEK_LINEAR_BACKEND  default: empty (vLLM auto-selection)
DEEPSEEK_DRY_RUN         default: 0
```

`DEEPSEEK_DRY_RUN=1` prints the fully escaped `vllm serve` command without
starting a process. Backend names are validated before use. Do not set the
local NVFP4 target's MoE backend to `flashinfer_b12x`; the engine will reject
it because DeepSeek V4 requires the SwiGLU clamp.
