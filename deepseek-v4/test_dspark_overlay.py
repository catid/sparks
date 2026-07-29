#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "bin"
    / "build_dspark_nvfp4_overlay.py"
)
SPEC = importlib.util.spec_from_file_location("dspark_overlay", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
overlay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = overlay
SPEC.loader.exec_module(overlay)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def write_safetensors(path: Path, tensors: dict[str, bytes]) -> None:
    offset = 0
    header: dict[str, dict] = {}
    payload = bytearray()
    for name, value in sorted(tensors.items()):
        header[name] = {
            "dtype": "I8",
            "shape": [len(value)],
            "data_offsets": [offset, offset + len(value)],
        }
        payload.extend(value)
        offset += len(value)
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    padding = (-len(raw)) % 8
    raw += b" " * padding
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + payload)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DSparkOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "source"
        self.artifacts = root / "artifacts"
        self.output = root / "overlay"
        self.source.mkdir()
        self.artifacts.mkdir()

        common = {field: f"value-{field}" for field in overlay.COMPATIBILITY_FIELDS}
        common.update(
            {
                "hidden_size": 4,
                "hc_mult": 4,
                "num_hidden_layers": 43,
                "n_routed_experts": 2,
                "vocab_size": 8,
                "expert_dtype": "fp4",
                "compress_ratios": [0, 0, 4],
            }
        )
        source_config = {
            **common,
            "architectures": ["DeepseekV4ForCausalLM"],
            "quantization_config": {
                "quant_method": "fp8",
                "quant_algo": "MIXED_PRECISION",
                "moe_quant_algo": "NVFP4",
                "ignore": ["mtp.*"],
            },
        }
        dspark_config = {
            **common,
            "architectures": ["DeepseekV4ForCausalLM"],
            **overlay.EXPECTED_DSPARK_FIELDS,
            "quantization_config": {"quant_method": "fp8"},
        }
        write_json(self.source / "config.json", source_config)
        write_json(self.artifacts / "config.json", dspark_config)
        (self.source / "tokenizer.json").write_text("{}", encoding="utf-8")

        self.base_shard = "model-00001-of-00002.safetensors"
        # The builder deliberately pins the exact source MTP layout as a
        # guardrail against merging an unexpected checkpoint.
        self.old_mtp_shard = "model-00046-of-00046.safetensors"
        write_safetensors(
            self.source / self.base_shard,
            {"layers.0.weight": b"base"},
        )
        write_safetensors(
            self.source / self.old_mtp_shard,
            {"mtp.0.old": b"old"},
        )
        write_json(
            self.source / "model.safetensors.index.json",
            {
                "metadata": {"total_size": 7},
                "weight_map": {
                    "layers.0.weight": self.base_shard,
                    "mtp.0.old": self.old_mtp_shard,
                },
            },
        )

        draft_map = {}
        for stage, shard in enumerate(overlay.DRAFT_SHARDS):
            key = f"mtp.{stage}.weight"
            write_safetensors(self.artifacts / shard, {key: bytes([stage + 1])})
            draft_map[key] = shard
        write_json(
            self.artifacts / "model.safetensors.index.json",
            {"metadata": {"total_size": 3}, "weight_map": draft_map},
        )

        self.file_specs = {}
        for name in overlay.FILE_SPECS:
            path = self.artifacts / name
            self.file_specs[name] = overlay.FileSpec(
                path.stat().st_size,
                sha256(path),
                overlay.FILE_SPECS[name].role,
            )

        self.patches = (
            mock.patch.object(overlay, "FILE_SPECS", self.file_specs),
            mock.patch.object(overlay, "SOURCE_MTP_COUNT", 1),
            mock.patch.object(overlay, "DRAFT_STAGE_COUNTS", {0: 1, 1: 1, 2: 1}),
        )
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_build_is_non_destructive_atomic_and_idempotent(self) -> None:
        before = {
            path.name: sha256(path)
            for path in (
                self.source / "config.json",
                self.source / "model.safetensors.index.json",
                self.source / self.base_shard,
                self.source / self.old_mtp_shard,
            )
        }

        overlay.build(self.source, self.artifacts, self.output)
        overlay.validate_overlay(self.output, self.source, self.artifacts)
        # A second build validates and reuses the existing tree.
        overlay.build(self.source, self.artifacts, self.output)

        after = {
            path.name: sha256(path)
            for path in (
                self.source / "config.json",
                self.source / "model.safetensors.index.json",
                self.source / self.base_shard,
                self.source / self.old_mtp_shard,
            )
        }
        self.assertEqual(before, after)

        index = json.loads(
            (self.output / "model.safetensors.index.json").read_text()
        )
        self.assertEqual(
            set(index["weight_map"]),
            {
                "layers.0.weight",
                "mtp.0.weight",
                "mtp.1.weight",
                "mtp.2.weight",
            },
        )
        self.assertNotIn(self.old_mtp_shard, index["weight_map"].values())
        self.assertTrue((self.output / self.base_shard).is_symlink())
        for shard in overlay.DRAFT_SHARDS:
            self.assertTrue(
                (self.output / overlay.DRAFT_OUTPUT_NAMES[shard]).is_symlink()
            )

        source_quant = json.loads((self.source / "config.json").read_text())[
            "quantization_config"
        ]
        merged_config = json.loads((self.output / "config.json").read_text())
        self.assertEqual(merged_config["quantization_config"], source_quant)
        for field, expected in overlay.EXPECTED_DSPARK_FIELDS.items():
            self.assertEqual(merged_config[field], expected)
        source_ratios = json.loads((self.source / "config.json").read_text())[
            "compress_ratios"
        ]
        self.assertEqual(
            merged_config["compress_ratios"],
            source_ratios,
        )
        self.assertEqual(source_ratios, [0, 0, 4])

    def test_zero_only_dspark_compress_ratio_extension(self) -> None:
        source_config = json.loads((self.source / "config.json").read_text())
        dspark_config = json.loads((self.artifacts / "config.json").read_text())
        dspark_config["compress_ratios"] = source_config["compress_ratios"] + [0, 0]
        overlay._validate_config_compatibility(source_config, dspark_config)

        dspark_config["compress_ratios"][-1] = 4
        with self.assertRaisesRegex(overlay.OverlayError, "compress_ratios"):
            overlay._validate_config_compatibility(source_config, dspark_config)

    def test_bad_pinned_shard_is_rejected(self) -> None:
        shard = self.artifacts / overlay.DRAFT_SHARDS[1]
        shard.write_bytes(shard.read_bytes() + b"corrupt")
        with self.assertRaisesRegex(overlay.OverlayError, "Wrong size"):
            overlay.validate_inputs(self.source, self.artifacts)
        self.assertFalse(self.output.exists())

    def test_output_filename_collision_is_rejected(self) -> None:
        collision_names = dict(overlay.DRAFT_OUTPUT_NAMES)
        collision_names[overlay.DRAFT_SHARDS[0]] = self.base_shard
        with mock.patch.object(overlay, "DRAFT_OUTPUT_NAMES", collision_names):
            with self.assertRaisesRegex(overlay.OverlayError, "filename collision"):
                overlay.validate_inputs(self.source, self.artifacts)

    def test_symlinked_pinned_artifact_is_rejected(self) -> None:
        config = self.artifacts / "config.json"
        real_config = self.artifacts / "config.real.json"
        config.rename(real_config)
        config.symlink_to(real_config)
        with self.assertRaisesRegex(overlay.OverlayError, "regular pinned artifact"):
            overlay.validate_inputs(self.source, self.artifacts)

    def test_unsafe_index_shard_path_is_rejected(self) -> None:
        index_path = self.source / "model.safetensors.index.json"
        index = json.loads(index_path.read_text())
        index["weight_map"]["layers.0.weight"] = "../outside.safetensors"
        write_json(index_path, index)
        with self.assertRaisesRegex(overlay.OverlayError, "unsafe shard filenames"):
            overlay.validate_inputs(self.source, self.artifacts)


if __name__ == "__main__":
    unittest.main()
