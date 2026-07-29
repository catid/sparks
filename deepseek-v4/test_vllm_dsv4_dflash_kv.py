#!/usr/bin/env python3
"""Regression tests for the local vLLM DeepSeek-V4 + DFlash KV patch."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import torch
from safetensors import safe_open

from vllm.models.deepseek_v4.nvidia.model import _format_aux_hidden_state
from vllm.transformers_utils.config import get_config
from vllm.v1.core import kv_cache_utils as kv
from vllm.v1.kv_cache_interface import (
    KVCacheConfig,
    MLAAttentionSpec,
    SlidingWindowMLASpec,
    SlidingWindowSpec,
)


class _DFlashConfig:
    def use_dflash(self) -> bool:
        return True

    def use_eagle(self) -> bool:
        return True


def _vllm_config(*, dflash: bool = True):
    speculative_config = (
        _DFlashConfig()
        if dflash
        else SimpleNamespace(
            use_dflash=lambda: False,
            use_eagle=lambda: False,
        )
    )
    return SimpleNamespace(
        scheduler_config=SimpleNamespace(
            disable_hybrid_kv_cache_manager=False,
            max_num_batched_tokens=32768,
        ),
        speculative_config=speculative_config,
        kv_transfer_config=None,
        cache_config=SimpleNamespace(num_gpu_blocks_override=None),
        model_config=SimpleNamespace(max_model_len=8192),
        parallel_config=SimpleNamespace(
            decode_context_parallel_size=1,
            prefill_context_parallel_size=1,
        ),
    )


def _representative_specs():
    specs = {
        "model.layers.0.self_attn.attn": MLAAttentionSpec(
            block_size=256,
            num_kv_heads=1,
            head_size=512,
            dtype=torch.uint8,
            cache_dtype_str="fp8_ds_mla",
            model_version="deepseek_v4",
        ),
        "model.layers.1.self_attn.attn": SlidingWindowMLASpec(
            block_size=64,
            num_kv_heads=1,
            head_size=512,
            dtype=torch.uint8,
            sliding_window=128,
            cache_dtype_str="fp8_ds_mla",
            model_version="deepseek_v4",
        ),
    }
    for layer_idx in range(43, 48):
        specs[f"model.layers.{layer_idx}.self_attn.attn"] = SlidingWindowSpec(
            block_size=16,
            num_kv_heads=1,
            head_size=256,
            dtype=torch.bfloat16,
            sliding_window=2048,
        )
    return specs


class TestDeepSeekV4DFlashKV(unittest.TestCase):
    def test_dflash_preserves_all_mhc_aux_streams(self):
        aux_recon = torch.arange(
            2 * 4 * 3,
            dtype=torch.bfloat16,
        ).reshape(2, 4, 3)
        dflash_state = _format_aux_hidden_state(
            aux_recon,
            preserve_mhc_streams=True,
        )
        legacy_state = _format_aux_hidden_state(
            aux_recon,
            preserve_mhc_streams=False,
        )
        self.assertEqual(dflash_state.shape, (2, 12))
        self.assertTrue(torch.equal(dflash_state, aux_recon.flatten(1)))
        self.assertEqual(legacy_state.shape, (2, 3))
        self.assertTrue(torch.equal(
            legacy_state,
            aux_recon.mean(dim=1),
        ))

    def test_dflash_target_width_matches_checkpoint_projection(self):
        draft_path = Path(
            "/home/catid/models/DeepSeek-V4-Flash-speculator.dflash"
        )
        raw_config = json.loads(
            (draft_path / "config.json").read_text(encoding="utf-8")
        )
        layer_config = raw_config["transformer_layer_config"]
        target_hidden_size = (
            layer_config["hidden_size"] * layer_config["hc_mult"]
        )
        self.assertEqual(
            raw_config["target_hidden_size"],
            target_hidden_size,
        )

        with safe_open(
            draft_path / "model.safetensors",
            framework="pt",
            device="cpu",
        ) as checkpoint:
            fc_shape = checkpoint.get_slice("fc.weight").get_shape()
        self.assertEqual(
            fc_shape,
            [
                layer_config["hidden_size"],
                target_hidden_size
                * len(raw_config["aux_hidden_state_layer_ids"]),
            ],
        )

        converted_config = get_config(
            str(draft_path),
            trust_remote_code=True,
        )
        self.assertEqual(
            converted_config.target_hidden_size,
            target_hidden_size,
        )

    def test_grouping_is_lossless_and_marks_only_draft_group(self):
        config = _vllm_config()
        specs = _representative_specs()
        groups = kv.get_kv_cache_groups(config, specs)

        assigned = [name for group in groups for name in group.layer_names]
        self.assertCountEqual(assigned, specs)
        self.assertEqual(len(assigned), len(set(assigned)))

        draft_names = set(list(specs)[-5:])
        draft_groups = [
            group for group in groups if set(group.layer_names) == draft_names
        ]
        self.assertEqual(len(draft_groups), 1)
        self.assertTrue(draft_groups[0].is_eagle_group)
        self.assertEqual(sum(group.is_eagle_group for group in groups), 1)

    def test_remaining_layers_are_not_accepted_outside_dflash(self):
        with self.assertRaisesRegex(
            NotImplementedError,
            "outside a DFlash configuration",
        ):
            kv.get_kv_cache_groups(
                _vllm_config(dflash=False),
                _representative_specs(),
            )

    def test_packed_memory_matches_allocator_stride(self):
        config = _vllm_config()
        groups = kv.get_kv_cache_groups(config, _representative_specs())
        stride = kv._pool_bytes_per_block(config, groups)
        expected = stride * sum(
            group.kv_cache_spec.max_memory_usage_pages(config)
            for group in groups
        )
        self.assertEqual(
            kv._max_memory_usage_bytes_from_groups(config, groups),
            expected,
        )

        expected_blocks = 1000
        cache_config = kv.get_kv_cache_config_from_groups(
            config,
            groups,
            stride * expected_blocks,
        )
        self.assertEqual(cache_config.num_blocks, expected_blocks)
        self.assertTrue(cache_config.kv_cache_tensors)

        per_layer_specs = {}
        for group in groups:
            per_layer_specs.update(group.kv_cache_spec.kv_cache_specs)
        for tensor in cache_config.kv_cache_tensors:
            self.assertEqual(tensor.size, stride * expected_blocks)
            self.assertEqual(tensor.block_stride, stride)
            largest_page = max(
                per_layer_specs[layer_name].page_size_bytes
                for layer_name in tensor.shared_by
            )
            self.assertLessEqual(tensor.offset + largest_page, stride)

    def test_worker_and_scheduler_capacity_agree(self):
        config = _vllm_config()
        groups = kv.get_kv_cache_groups(config, _representative_specs())
        worker_config = KVCacheConfig(
            num_blocks=10000,
            kv_cache_tensors=[],
            kv_cache_groups=groups,
        )
        scheduler_config = kv.generate_scheduler_kv_cache_config(
            [copy.deepcopy(worker_config)]
        )
        worker_concurrency = kv.get_max_concurrency_for_kv_cache_config(
            config, worker_config
        )
        scheduler_concurrency = kv.get_max_concurrency_for_kv_cache_config(
            config, scheduler_config
        )
        self.assertEqual(worker_concurrency, scheduler_concurrency)


if __name__ == "__main__":
    unittest.main()
