#!/usr/bin/env python3
"""Derive and verify the immutable identity of the Audio8 SGLang runtime."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import stat
import sys
from dataclasses import dataclass
from typing import Any


BASE_IMAGE_LABEL = "io.cerberus.audio8-sglang.base-image"
SGLANG_COMMIT_LABEL = "io.cerberus.audio8-sglang.sglang-omni-commit"
AUDIO8_COMMIT_LABEL = "io.cerberus.audio8-sglang.audio8-tts-commit"
FINGERPRINT_LABEL = (
    "io.cerberus.audio8-sglang.source-contract-patchset-sha256"
)
HEX_40 = re.compile(r"[0-9a-f]{40}\Z")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
BASE_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RuntimeIdentity:
    base_image: str
    sglang_omni_commit: str
    audio8_tts_commit: str
    image: str
    model_directory: str
    model_revision: str
    served_model_name: str
    fingerprint: str

    def labels(self) -> dict[str, str]:
        return {
            BASE_IMAGE_LABEL: self.base_image,
            SGLANG_COMMIT_LABEL: self.sglang_omni_commit,
            AUDIO8_COMMIT_LABEL: self.audio8_tts_commit,
            FINGERPRINT_LABEL: self.fingerprint,
        }

    def values(self) -> tuple[str, ...]:
        return (
            self.base_image,
            self.sglang_omni_commit,
            self.audio8_tts_commit,
            self.image,
            self.model_directory,
            self.model_revision,
            self.served_model_name,
            self.fingerprint,
        )


def file_sha256(path: pathlib.Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"runtime identity input is not a regular file: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def required_string(lock: dict[str, Any], key: str) -> str:
    value = lock.get(key)
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"runtime lock has an invalid {key}")
    return value


def load_runtime_identity(
    lock_path: pathlib.Path, artifact_root: pathlib.Path
) -> RuntimeIdentity:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(lock, dict):
        raise ValueError("runtime lock must be a JSON object")

    base_image = required_string(lock, "base_image")
    sglang_commit = required_string(lock, "sglang_omni_commit")
    audio8_commit = required_string(lock, "audio8_tts_commit")
    image = required_string(lock, "image")
    model_directory = required_string(lock, "model_directory")
    model_revision = required_string(lock, "model_revision")
    served_model_name = required_string(lock, "served_model_name")
    if BASE_IMAGE.fullmatch(base_image) is None:
        raise ValueError("runtime lock base_image must use an immutable sha256 digest")
    if HEX_40.fullmatch(sglang_commit) is None:
        raise ValueError("runtime lock has an invalid sglang_omni_commit")
    if HEX_40.fullmatch(audio8_commit) is None:
        raise ValueError("runtime lock has an invalid audio8_tts_commit")
    if HEX_40.fullmatch(model_revision) is None:
        raise ValueError("runtime lock has an invalid model_revision")

    source_contract = lock.get("single_process_source_contract")
    if not isinstance(source_contract, dict) or not source_contract:
        raise ValueError("runtime lock has an invalid single-process source contract")
    normalized_contract: dict[str, str] = {}
    for key, value in source_contract.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(value, str)
            or HEX_64.fullmatch(value) is None
        ):
            raise ValueError("runtime lock has an invalid source-contract digest")
        normalized_contract[key] = value

    patches_dir = artifact_root / "patches"
    try:
        patch_paths = sorted(patches_dir.glob("*.patch"), key=lambda path: path.name)
    except OSError as error:
        raise ValueError("cannot enumerate the runtime patchset") from error
    if not patch_paths:
        raise ValueError("runtime patchset is empty")
    patchset = {
        f"patches/{path.name}": file_sha256(path) for path in patch_paths
    }
    runtime_artifacts = {
        name: file_sha256(artifact_root / name)
        for name in (
            "Dockerfile",
            "runtime_identity.py",
            "verify_source_contract.py",
        )
    }
    fingerprint_document = {
        "schema": 1,
        "base_image": base_image,
        "sglang_omni_commit": sglang_commit,
        "audio8_tts_commit": audio8_commit,
        "model_revision": model_revision,
        "served_model_name": served_model_name,
        "single_process_source_contract": normalized_contract,
        "runtime_artifacts": runtime_artifacts,
        "patchset": patchset,
    }
    encoded = json.dumps(
        fingerprint_document,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    return RuntimeIdentity(
        base_image=base_image,
        sglang_omni_commit=sglang_commit,
        audio8_tts_commit=audio8_commit,
        image=image,
        model_directory=model_directory,
        model_revision=model_revision,
        served_model_name=served_model_name,
        fingerprint=fingerprint,
    )


def verify_labels(identity: RuntimeIdentity, labels: Any) -> None:
    if not isinstance(labels, dict):
        raise ValueError("image has no OCI labels")
    for key, expected in identity.labels().items():
        if labels.get(key) != expected:
            raise ValueError(f"image OCI label does not match runtime lock: {key}")


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] not in {"values", "verify-labels"}:
        raise SystemExit(
            "usage: runtime_identity.py values|verify-labels LOCK ARTIFACT_ROOT"
        )
    try:
        identity = load_runtime_identity(
            pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
        )
        if sys.argv[1] == "values":
            for value in identity.values():
                print(value)
            return
        labels = json.load(sys.stdin)
        verify_labels(identity, labels)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid Audio8 SGLang runtime identity: {error}") from error


if __name__ == "__main__":
    main()
