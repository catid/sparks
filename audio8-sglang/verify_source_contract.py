#!/usr/bin/env python3
"""Fail a build if the pinned single-process attestation contract changed."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


CONTRACT_FILES = {
    "sglang_launcher_sha256": (
        "sglang",
        "sglang_omni/serve/launcher.py",
    ),
    "sglang_compiler_sha256": (
        "sglang",
        "sglang_omni/config/compiler.py",
    ),
    "audio8_config_sha256": (
        "audio8",
        "sglang_omni/configs/audio8_tts_0_6b.yaml",
    ),
}


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_source_contract(
    lock_path: pathlib.Path,
    sglang_root: pathlib.Path,
    audio8_root: pathlib.Path,
) -> None:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected = lock.get("single_process_source_contract")
    if not isinstance(expected, dict) or set(expected) != set(CONTRACT_FILES):
        raise ValueError("runtime lock has an invalid source contract")
    roots = {"sglang": sglang_root, "audio8": audio8_root}
    sources: dict[str, str] = {}
    for key, (root_name, relative_path) in CONTRACT_FILES.items():
        path = roots[root_name] / relative_path
        if file_sha256(path) != expected[key]:
            raise ValueError(f"pinned source contract hash changed: {relative_path}")
        sources[key] = path.read_text(encoding="utf-8")

    launcher = sources["sglang_launcher_sha256"]
    compiler = sources["sglang_compiler_sha256"]
    config = sources["audio8_config_sha256"]
    required_launcher_fragments = (
        "need_multi_process = len(gpu_ids) > 1",
        "runner = build_pipeline_runner(pipeline_config)",
        "app = create_app(client, model_name=model_name)",
    )
    if any(fragment not in launcher for fragment in required_launcher_fragments):
        raise ValueError("SGLang launcher no longer guarantees the reviewed path")
    if "executor = factory(**stage_cfg.executor.args)" not in compiler:
        raise ValueError("SGLang executor construction contract changed")
    active_config = "\n".join(
        line for line in config.splitlines() if not line.lstrip().startswith("#")
    )
    if "gpu_placement:" in active_config:
        raise ValueError("Audio8 config unexpectedly selects multi-process placement")
    if active_config.count("device: cuda:0") != 3:
        raise ValueError("Audio8 stages no longer share the reviewed GPU process")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: verify_source_contract.py LOCK SGLANG_ROOT AUDIO8_ROOT"
        )
    try:
        verify_source_contract(
            pathlib.Path(sys.argv[1]),
            pathlib.Path(sys.argv[2]),
            pathlib.Path(sys.argv[3]),
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid Audio8 source contract: {error}") from error


if __name__ == "__main__":
    main()
