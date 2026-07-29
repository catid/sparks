#!/usr/bin/env python3
"""Build a non-destructive DeepSeek V4 NVFP4 + DSpark checkpoint overlay.

The NVIDIA target checkpoint is left untouched. Base-model files are exposed
through symlinks, while only the three official DeepSeek DSpark MTP shards are
added. The Hugging Face revision, byte sizes, and SHA-256 digests are pinned.

This tool deliberately separates planning, fetching, and building:

  build_dspark_nvfp4_overlay.py plan
  build_dspark_nvfp4_overlay.py fetch-metadata
  build_dspark_nvfp4_overlay.py fetch
  build_dspark_nvfp4_overlay.py build
  build_dspark_nvfp4_overlay.py validate

The resulting overlay is structurally complete, but is marked experimental:
the NVIDIA target's routed experts are ModelOpt NVFP4 while the official
DSpark MTP routed experts remain DeepSeek-native MXFP4. See
deepseek-v4/DSPARK_OVERLAY.md before attempting to serve it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REPO_ID = "deepseek-ai/DeepSeek-V4-Flash-DSpark"
REVISION = "62af8fffb2f7030cac4de2f0169f5b8d1101b646"
DEFAULT_SOURCE = Path("/home/catid/models/DeepSeek-V4-Flash-NVFP4")
DEFAULT_ARTIFACTS = (
    Path("/home/catid/models/.dspark-artifacts") / REPO_ID.replace("/", "--") / REVISION
)
DEFAULT_OUTPUT = Path(
    "/home/catid/models/DeepSeek-V4-Flash-NVFP4-DSpark-overlay"
)


class OverlayError(RuntimeError):
    """A validation or safety failure."""


@dataclass(frozen=True)
class FileSpec:
    size: int
    sha256: str
    role: str


FILE_SPECS: dict[str, FileSpec] = {
    "config.json": FileSpec(
        1_888,
        "6c8f3d2d3b48707541b88f32f22ef3f0f8a6b57d8523281e2b8d3cdb0ae9a023",
        "metadata",
    ),
    "model.safetensors.index.json": FileSpec(
        5_602_871,
        "98efab455cf08dfbbbaaba6f570e1bf10bf927d2b4c3c453a59c2f6f0e3be92b",
        "metadata",
    ),
    "model-00046-of-00048.safetensors": FileSpec(
        3_610_455_184,
        "14810f274692bb771c3970e8cba45846c4aa2213dcfb0025ffebe788d229e18d",
        "mtp.0",
    ),
    "model-00047-of-00048.safetensors": FileSpec(
        3_560_111_960,
        "7a44164698d90648a35c030c5eb369256d2c469306bfbf2b1ae27f35b6e57889",
        "mtp.1",
    ),
    "model-00048-of-00048.safetensors": FileSpec(
        3_692_775_244,
        "a0bbb24f36d2ef6107250088e0f020f93aec0677cd24be3e9e69589547a7656f",
        "mtp.2",
    ),
}

METADATA_FILES = ("config.json", "model.safetensors.index.json")
DRAFT_SHARDS = (
    "model-00046-of-00048.safetensors",
    "model-00047-of-00048.safetensors",
    "model-00048-of-00048.safetensors",
)
DRAFT_OUTPUT_NAMES = {
    name: f"dspark-{name}" for name in DRAFT_SHARDS
}
SOURCE_MTP_COUNT = 1_575
DRAFT_STAGE_COUNTS = {0: 1_568, 1: 1_565, 2: 1_572}
DSPARK_FIELDS = (
    "dspark_block_size",
    "dspark_noise_token_id",
    "dspark_target_layer_ids",
    "dspark_markov_rank",
)
COMPATIBILITY_FIELDS = (
    "architectures",
    "model_type",
    "hidden_size",
    "hc_mult",
    "hc_eps",
    "hc_sinkhorn_iters",
    "num_hidden_layers",
    "n_routed_experts",
    "moe_intermediate_size",
    "num_attention_heads",
    "num_key_value_heads",
    "head_dim",
    "vocab_size",
    "expert_dtype",
    "num_experts_per_tok",
    "n_shared_experts",
    "sliding_window",
    "num_hash_layers",
    "index_head_dim",
    "index_n_heads",
    "index_topk",
    "o_groups",
    "o_lora_rank",
    "q_lora_rank",
    "qk_rope_head_dim",
    "rms_norm_eps",
    "rope_scaling",
    "rope_theta",
    "compress_rope_theta",
    "swiglu_limit",
    "hidden_act",
    "attention_bias",
    "attention_dropout",
    "norm_topk_prob",
    "routed_scaling_factor",
    "scoring_func",
    "topk_method",
    "num_nextn_predict_layers",
    "max_position_embeddings",
    "tie_word_embeddings",
    "bos_token_id",
    "eos_token_id",
)
EXPECTED_DSPARK_FIELDS = {
    "dspark_block_size": 5,
    "dspark_noise_token_id": 128_799,
    "dspark_target_layer_ids": [40, 41, 42],
    "dspark_markov_rank": 256,
}
RUNTIME_STATUS = "structurally_valid_runtime_experimental"
RUNTIME_WARNING = (
    "The target uses NVIDIA ModelOpt NVFP4 routed experts, but the imported "
    "DSpark MTP shards retain DeepSeek-native MXFP4 expert tensors. vLLM "
    "0.25.1 has the DSpark loader, but this heterogeneous quantization mix "
    "has not been validated and must not be treated as production-ready."
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_load(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise OverlayError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OverlayError(f"Expected a JSON object in {path}")
    return value


def _json_write(path: Path, value: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _validate_manifest_file(path: Path, spec: FileSpec) -> None:
    if path.is_symlink() or not path.is_file():
        raise OverlayError(f"Missing regular pinned artifact: {path}")
    actual_size = path.stat().st_size
    if actual_size != spec.size:
        raise OverlayError(
            f"Wrong size for {path}: got {actual_size}, expected {spec.size}"
        )
    actual_sha = _sha256(path)
    if actual_sha != spec.sha256:
        raise OverlayError(
            f"Wrong SHA-256 for {path}: got {actual_sha}, expected {spec.sha256}"
        )


def _download_url(name: str) -> str:
    return f"https://huggingface.co/{REPO_ID}/resolve/{REVISION}/{name}"


def _quarantine(path: Path, reason: str) -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.invalid-{reason}-{stamp}")
    counter = 0
    while os.path.lexists(candidate):
        counter += 1
        candidate = path.with_name(f"{path.name}.invalid-{reason}-{stamp}-{counter}")
    path.rename(candidate)
    return candidate


def _fetch_one(artifact_dir: Path, name: str) -> None:
    spec = FILE_SPECS[name]
    destination = artifact_dir / name
    partial = artifact_dir / f".{name}.part"

    if os.path.lexists(destination):
        if destination.is_symlink() or not destination.is_file():
            raise OverlayError(f"Refusing non-regular artifact path: {destination}")
        _validate_manifest_file(destination, spec)
        print(f"verified existing {destination}")
        return

    artifact_dir.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(partial) and (
        partial.is_symlink() or not partial.is_file()
    ):
        raise OverlayError(f"Refusing non-regular partial download: {partial}")
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > spec.size:
        moved = _quarantine(partial, "oversize")
        print(f"quarantined oversized partial download as {moved}", file=sys.stderr)
        offset = 0

    headers = {"User-Agent": "dgx-spark-laguna-dspark-overlay/1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(_download_url(name), headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=120)
    except urllib.error.URLError as exc:
        raise OverlayError(f"Download failed for {name}: {exc}") from exc

    status = getattr(response, "status", response.getcode())
    if offset and status != 206:
        response.close()
        moved = _quarantine(partial, "range-ignored")
        print(f"server ignored resume; preserved partial as {moved}", file=sys.stderr)
        offset = 0
        request = urllib.request.Request(
            _download_url(name),
            headers={"User-Agent": "dgx-spark-laguna-dspark-overlay/1"},
        )
        try:
            response = urllib.request.urlopen(request, timeout=120)
        except urllib.error.URLError as exc:
            raise OverlayError(f"Download failed for {name}: {exc}") from exc
        status = getattr(response, "status", response.getcode())
    if status not in (200, 206):
        response.close()
        raise OverlayError(f"Unexpected HTTP {status} while fetching {name}")

    mode = "ab" if offset and status == 206 else "wb"
    print(
        f"fetching {name}: {spec.size:,} bytes"
        + (f" (resuming at {offset:,})" if offset else "")
    )
    try:
        with response, partial.open(mode) as handle:
            while True:
                chunk = response.read(8 * 1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                if handle.tell() > spec.size:
                    raise OverlayError(f"Download exceeded pinned size for {name}")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise OverlayError(f"Cannot write {partial}: {exc}") from exc

    _validate_manifest_file(partial, spec)
    os.replace(partial, destination)
    print(f"stored and verified {destination}")


def fetch(artifact_dir: Path, *, metadata_only: bool) -> None:
    names = METADATA_FILES if metadata_only else tuple(FILE_SPECS)
    for name in names:
        _fetch_one(artifact_dir, name)


def _weight_map(index: dict[str, Any], path: Path) -> dict[str, str]:
    mapping = index.get("weight_map")
    if not isinstance(mapping, dict) or not mapping:
        raise OverlayError(f"Missing non-empty weight_map in {path}")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in mapping.items()
    ):
        raise OverlayError(f"weight_map must contain only string pairs in {path}")
    unsafe_names = sorted(
        {
            value
            for value in mapping.values()
            if Path(value).name != value or value in (".", "..")
        }
    )
    if unsafe_names:
        raise OverlayError(
            f"weight_map contains unsafe shard filenames in {path}: "
            f"{unsafe_names[:5]}"
        )
    return mapping


def _read_safetensors_header(path: Path) -> tuple[dict[str, Any], int]:
    try:
        with path.open("rb") as handle:
            raw_len = handle.read(8)
            if len(raw_len) != 8:
                raise OverlayError(f"Truncated safetensors header in {path}")
            header_len = struct.unpack("<Q", raw_len)[0]
            if header_len <= 1 or header_len > 64 * 1024 * 1024:
                raise OverlayError(
                    f"Unsafe safetensors header length {header_len} in {path}"
                )
            raw_header = handle.read(header_len)
    except OSError as exc:
        raise OverlayError(f"Cannot read safetensors file {path}: {exc}") from exc
    if len(raw_header) != header_len:
        raise OverlayError(f"Truncated safetensors JSON header in {path}")
    try:
        header = json.loads(raw_header)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OverlayError(f"Invalid safetensors JSON header in {path}: {exc}") from exc
    if not isinstance(header, dict):
        raise OverlayError(f"Safetensors header is not an object in {path}")
    return header, header_len


def _validate_shard(path: Path, expected_keys: set[str]) -> int:
    header, header_len = _read_safetensors_header(path)
    tensor_header = {key: value for key, value in header.items() if key != "__metadata__"}
    actual_keys = set(tensor_header)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)[:5]
        extra = sorted(actual_keys - expected_keys)[:5]
        raise OverlayError(
            f"Index/header key mismatch for {path}; missing={missing}, extra={extra}"
        )

    data_size = path.stat().st_size - 8 - header_len
    intervals: list[tuple[int, int, str]] = []
    tensor_bytes = 0
    for key, entry in tensor_header.items():
        if not isinstance(entry, dict):
            raise OverlayError(f"Bad tensor entry {key!r} in {path}")
        offsets = entry.get("data_offsets")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(isinstance(value, int) for value in offsets)
        ):
            raise OverlayError(f"Bad data_offsets for {key!r} in {path}")
        start, end = offsets
        if start < 0 or end < start or end > data_size:
            raise OverlayError(
                f"Out-of-range data_offsets {offsets} for {key!r} in {path}"
            )
        if not isinstance(entry.get("dtype"), str) or not isinstance(
            entry.get("shape"), list
        ):
            raise OverlayError(f"Missing dtype/shape for {key!r} in {path}")
        intervals.append((start, end, key))
        tensor_bytes += end - start

    intervals.sort()
    previous_end = 0
    for start, end, key in intervals:
        if start < previous_end:
            raise OverlayError(f"Overlapping tensor {key!r} in {path}")
        if start != previous_end:
            raise OverlayError(
                f"Unindexed data gap before tensor {key!r} in {path}: "
                f"{previous_end}..{start}"
            )
        previous_end = end
    if intervals and intervals[-1][1] != data_size:
        raise OverlayError(
            f"Safetensors data does not end at EOF in {path}: "
            f"{intervals[-1][1]} != {data_size}"
        )
    return tensor_bytes


@dataclass
class InputLayout:
    source_config: dict[str, Any]
    dspark_config: dict[str, Any]
    source_index: dict[str, Any]
    dspark_index: dict[str, Any]
    source_base_map: dict[str, str]
    dspark_mtp_map: dict[str, str]
    merged_map: dict[str, str]
    total_tensor_bytes: int
    source_config_sha256: str
    source_index_sha256: str


def _validate_config_compatibility(
    source_config: dict[str, Any], dspark_config: dict[str, Any]
) -> None:
    mismatches = []
    for field in COMPATIBILITY_FIELDS:
        if source_config.get(field) != dspark_config.get(field):
            mismatches.append(
                f"{field}: target={source_config.get(field)!r}, "
                f"dspark={dspark_config.get(field)!r}"
            )
    if mismatches:
        raise OverlayError("Checkpoint architecture mismatch: " + "; ".join(mismatches))

    source_ratios = source_config.get("compress_ratios")
    dspark_ratios = dspark_config.get("compress_ratios")
    if (
        not isinstance(source_ratios, list)
        or not isinstance(dspark_ratios, list)
        or dspark_ratios[: len(source_ratios)] != source_ratios
        or any(value != 0 for value in dspark_ratios[len(source_ratios) :])
    ):
        raise OverlayError(
            "DSpark compress_ratios must preserve the target prefix and may "
            "only append zero-valued draft-layer entries"
        )

    for field, expected in EXPECTED_DSPARK_FIELDS.items():
        if dspark_config.get(field) != expected:
            raise OverlayError(
                f"Unexpected official DSpark {field}: "
                f"{dspark_config.get(field)!r} != {expected!r}"
            )

    quant = source_config.get("quantization_config")
    if not isinstance(quant, dict):
        raise OverlayError("Target lacks quantization_config")
    if str(quant.get("moe_quant_algo", "")).upper() != "NVFP4":
        raise OverlayError("Target is not the expected NVIDIA NVFP4 MoE checkpoint")
    ignored = quant.get("ignore")
    if not isinstance(ignored, list) or "mtp.*" not in ignored:
        raise OverlayError(
            "Target quantization config does not preserve native MTP weights"
        )


def validate_inputs(source: Path, artifact_dir: Path) -> InputLayout:
    source = source.resolve()
    if not source.is_dir():
        raise OverlayError(f"Source checkpoint is not a directory: {source}")

    for name, spec in FILE_SPECS.items():
        _validate_manifest_file(artifact_dir / name, spec)

    source_config_path = source / "config.json"
    source_index_path = source / "model.safetensors.index.json"
    source_config = _json_load(source_config_path)
    source_index = _json_load(source_index_path)
    dspark_config = _json_load(artifact_dir / "config.json")
    dspark_index = _json_load(artifact_dir / "model.safetensors.index.json")
    _validate_config_compatibility(source_config, dspark_config)

    source_map = _weight_map(source_index, source_index_path)
    dspark_map = _weight_map(
        dspark_index, artifact_dir / "model.safetensors.index.json"
    )
    source_mtp = {key: value for key, value in source_map.items() if key.startswith("mtp.")}
    if len(source_mtp) != SOURCE_MTP_COUNT or set(source_mtp.values()) != {
        "model-00046-of-00046.safetensors"
    }:
        raise OverlayError(
            "Target MTP layout is not the expected single native mtp.0 shard"
        )
    source_base = {
        key: value for key, value in source_map.items() if not key.startswith("mtp.")
    }

    dspark_mtp = {
        key: value for key, value in dspark_map.items() if key.startswith("mtp.")
    }
    if set(dspark_mtp.values()) != set(DRAFT_SHARDS):
        raise OverlayError(
            "Official MTP map does not resolve exclusively to pinned shards 46-48"
        )
    for stage, expected_count in DRAFT_STAGE_COUNTS.items():
        prefix = f"mtp.{stage}."
        stage_items = {
            key: value for key, value in dspark_mtp.items() if key.startswith(prefix)
        }
        expected_shard = DRAFT_SHARDS[stage]
        if len(stage_items) != expected_count or set(stage_items.values()) != {
            expected_shard
        }:
            raise OverlayError(
                f"Unexpected {prefix} layout: {len(stage_items)} tensors in "
                f"{set(stage_items.values())}, expected {expected_count} in "
                f"{expected_shard}"
            )
    if len(dspark_mtp) != sum(DRAFT_STAGE_COUNTS.values()):
        raise OverlayError("Unexpected DSpark MTP stage outside mtp.0/1/2")

    collisions = set(source_base).intersection(dspark_mtp)
    if collisions:
        raise OverlayError(f"Weight-name collision: {sorted(collisions)[:5]}")

    source_output_names = set(source_base.values())
    draft_output_names = set(DRAFT_OUTPUT_NAMES.values())
    filename_collisions = source_output_names.intersection(draft_output_names)
    if filename_collisions:
        raise OverlayError(
            f"Shard filename collision: {sorted(filename_collisions)}"
        )

    total_tensor_bytes = 0
    for shard_name in sorted(source_output_names):
        path = source / shard_name
        keys = {key for key, value in source_base.items() if value == shard_name}
        total_tensor_bytes += _validate_shard(path, keys)

    renamed_mtp: dict[str, str] = {}
    for shard_name in DRAFT_SHARDS:
        path = artifact_dir / shard_name
        keys = {key for key, value in dspark_mtp.items() if value == shard_name}
        total_tensor_bytes += _validate_shard(path, keys)
        output_name = DRAFT_OUTPUT_NAMES[shard_name]
        renamed_mtp.update({key: output_name for key in keys})

    merged_map = dict(source_base)
    merged_map.update(renamed_mtp)
    if len(merged_map) != len(source_base) + len(dspark_mtp):
        raise OverlayError("Merged weight map lost tensors")

    return InputLayout(
        source_config=source_config,
        dspark_config=dspark_config,
        source_index=source_index,
        dspark_index=dspark_index,
        source_base_map=source_base,
        dspark_mtp_map=dspark_mtp,
        merged_map=merged_map,
        total_tensor_bytes=total_tensor_bytes,
        source_config_sha256=_sha256(source_config_path),
        source_index_sha256=_sha256(source_index_path),
    )


def _merged_config(layout: InputLayout) -> dict[str, Any]:
    merged = json.loads(json.dumps(layout.source_config))
    for field in DSPARK_FIELDS:
        merged[field] = layout.dspark_config[field]
    # Preserve the NVIDIA config byte-for-byte at the subtree level. In
    # particular, never replace it with the official checkpoint's simpler FP8
    # config.
    if merged.get("quantization_config") != layout.source_config.get(
        "quantization_config"
    ):
        raise OverlayError("Internal error: target quantization config changed")
    return merged


def _provenance(source: Path, artifact_dir: Path, layout: InputLayout) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_checkpoint": str(source.resolve()),
        "source_config_sha256": layout.source_config_sha256,
        "source_index_sha256": layout.source_index_sha256,
        "dspark_repo_id": REPO_ID,
        "dspark_revision": REVISION,
        "artifacts_directory": str(artifact_dir.resolve()),
        "artifacts": {name: asdict(spec) for name, spec in FILE_SPECS.items()},
        "source_base_tensor_count": len(layout.source_base_map),
        "dspark_mtp_tensor_count": len(layout.dspark_mtp_map),
        "merged_tensor_count": len(layout.merged_map),
        "merged_total_tensor_bytes": layout.total_tensor_bytes,
        "runtime_status": RUNTIME_STATUS,
        "runtime_warning": RUNTIME_WARNING,
    }


def _link(source: Path, destination: Path) -> None:
    if os.path.lexists(destination):
        raise OverlayError(f"Refusing symlink collision at {destination}")
    destination.symlink_to(source.resolve(), target_is_directory=source.is_dir())


def _populate_stage(
    stage: Path, source: Path, artifact_dir: Path, layout: InputLayout
) -> None:
    source = source.resolve()
    artifact_dir = artifact_dir.resolve()

    excluded = {
        "config.json",
        "model.safetensors.index.json",
        ".cache",
        "cast_mxfp4_to_nvfp4.log",
    }
    for entry in sorted(source.iterdir()):
        if entry.name in excluded or (
            entry.name.startswith("model-") and entry.name.endswith(".safetensors")
        ):
            continue
        _link(entry, stage / entry.name)

    for shard_name in sorted(set(layout.source_base_map.values())):
        _link(source / shard_name, stage / shard_name)
    for shard_name in DRAFT_SHARDS:
        _link(
            artifact_dir / shard_name,
            stage / DRAFT_OUTPUT_NAMES[shard_name],
        )

    _json_write(stage / "config.json", _merged_config(layout))
    _json_write(
        stage / "model.safetensors.index.json",
        {
            "metadata": {"total_size": layout.total_tensor_bytes},
            "weight_map": dict(sorted(layout.merged_map.items())),
        },
    )
    _json_write(
        stage / ".dspark-overlay.json",
        _provenance(source, artifact_dir, layout),
    )


def validate_overlay(
    output: Path, source: Path, artifact_dir: Path, layout: InputLayout | None = None
) -> None:
    if output.is_symlink() or not output.is_dir():
        raise OverlayError(f"Overlay must be a real directory: {output}")
    if layout is None:
        layout = validate_inputs(source, artifact_dir)

    provenance = _json_load(output / ".dspark-overlay.json")
    expected_provenance = _provenance(source, artifact_dir, layout)
    for key in (
        "schema_version",
        "source_checkpoint",
        "source_config_sha256",
        "source_index_sha256",
        "dspark_repo_id",
        "dspark_revision",
        "artifacts",
        "source_base_tensor_count",
        "dspark_mtp_tensor_count",
        "merged_tensor_count",
        "merged_total_tensor_bytes",
        "runtime_status",
        "runtime_warning",
    ):
        if provenance.get(key) != expected_provenance.get(key):
            raise OverlayError(f"Overlay provenance mismatch for {key}")

    if _json_load(output / "config.json") != _merged_config(layout):
        raise OverlayError("Overlay config does not match the deterministic merge")
    merged_index = _json_load(output / "model.safetensors.index.json")
    if merged_index.get("metadata", {}).get("total_size") != layout.total_tensor_bytes:
        raise OverlayError("Overlay index total_size is wrong")
    if _weight_map(merged_index, output / "model.safetensors.index.json") != dict(
        sorted(layout.merged_map.items())
    ):
        raise OverlayError("Overlay weight map does not match the deterministic merge")

    for shard_name in sorted(set(layout.source_base_map.values())):
        link = output / shard_name
        if not link.is_symlink() or link.resolve() != (source / shard_name).resolve():
            raise OverlayError(f"Wrong base shard symlink: {link}")
    for shard_name in DRAFT_SHARDS:
        link = output / DRAFT_OUTPUT_NAMES[shard_name]
        if not link.is_symlink() or link.resolve() != (
            artifact_dir / shard_name
        ).resolve():
            raise OverlayError(f"Wrong DSpark shard symlink: {link}")

    # Re-check the complete merged index against every linked shard header.
    merged_map = _weight_map(merged_index, output / "model.safetensors.index.json")
    for shard_name in sorted(set(merged_map.values())):
        keys = {key for key, value in merged_map.items() if value == shard_name}
        _validate_shard(output / shard_name, keys)


def build(source: Path, artifact_dir: Path, output: Path) -> None:
    source = source.resolve()
    artifact_dir = artifact_dir.resolve()
    if output.resolve() in (source, artifact_dir):
        raise OverlayError("Output must differ from source and artifact directories")
    if source in output.resolve().parents or artifact_dir in output.resolve().parents:
        raise OverlayError(
            "Output must not be nested inside the source or artifact directory"
        )

    layout = validate_inputs(source, artifact_dir)
    output.parent.mkdir(parents=True, exist_ok=True)

    if os.path.lexists(output):
        validate_overlay(output, source, artifact_dir, layout)
        print(f"overlay already exists and is identical: {output}")
        print(f"runtime_status={RUNTIME_STATUS}")
        print(f"WARNING: {RUNTIME_WARNING}", file=sys.stderr)
        return

    stage_path = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.tmp-", dir=output.parent)
    )
    published = False
    try:
        _populate_stage(stage_path, source, artifact_dir, layout)
        validate_overlay(stage_path, source, artifact_dir, layout)
        os.rename(stage_path, output)
        published = True
    finally:
        if not published and stage_path.exists():
            # stage_path is a freshly-created, output-specific temporary
            # directory; no caller-controlled broad path is ever removed.
            shutil.rmtree(stage_path)

    print(f"published overlay atomically: {output}")
    print(f"merged tensors: {len(layout.merged_map):,}")
    print(f"merged tensor bytes: {layout.total_tensor_bytes:,}")
    print(f"runtime_status={RUNTIME_STATUS}")
    print(f"WARNING: {RUNTIME_WARNING}", file=sys.stderr)


def print_plan(source: Path, artifact_dir: Path, output: Path) -> None:
    shard_bytes = sum(FILE_SPECS[name].size for name in DRAFT_SHARDS)
    metadata_bytes = sum(FILE_SPECS[name].size for name in METADATA_FILES)
    total_bytes = shard_bytes + metadata_bytes
    print(f"repo={REPO_ID}")
    print(f"revision={REVISION}")
    print(f"source={source}")
    print(f"artifacts={artifact_dir}")
    print(f"output={output}")
    print("required artifacts:")
    for name, spec in FILE_SPECS.items():
        print(f"  {name}  {spec.size:,} bytes  sha256={spec.sha256}")
    print(f"three_shards_bytes={shard_bytes:,}")
    print(f"metadata_bytes={metadata_bytes:,}")
    print(f"total_download_bytes={total_bytes:,}")
    print(f"total_download_gib={total_bytes / (1024**3):.9f}")
    print(f"runtime_status={RUNTIME_STATUS}")
    print(f"WARNING: {RUNTIME_WARNING}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("plan", "fetch-metadata", "fetch", "build", "validate"),
        help="plan is read-only; fetch downloads pinned files; build publishes atomically",
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.action == "plan":
            print_plan(args.source, args.artifacts, args.output)
        elif args.action == "fetch-metadata":
            fetch(args.artifacts, metadata_only=True)
        elif args.action == "fetch":
            fetch(args.artifacts, metadata_only=False)
        elif args.action == "build":
            build(args.source, args.artifacts, args.output)
        elif args.action == "validate":
            layout = validate_inputs(args.source, args.artifacts)
            validate_overlay(args.output, args.source.resolve(), args.artifacts.resolve(), layout)
            print(f"overlay validation passed: {args.output}")
            print(f"runtime_status={RUNTIME_STATUS}")
            print(f"WARNING: {RUNTIME_WARNING}", file=sys.stderr)
        else:  # pragma: no cover - argparse makes this unreachable
            raise AssertionError(args.action)
    except OverlayError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
