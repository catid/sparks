#!/usr/bin/env python3
"""Remove only reviewed, unused Audio8 runtime caches as the service user."""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import sys

from runtime_identity import load_runtime_identity


CACHE_ROOT = pathlib.Path("/home/catid/.cache/cerberus-audio8-sglang")
KNOWN_OBSOLETE_FINGERPRINTS = frozenset(
    {
        "310c943848beedc12376b9b084e76192e29b33444bedaa215efd96bedd31826a",
        "2cf49c3ec3433f53e46c016813885dbaedc212d2b7b93e872f1e87070008b7b4",
    }
)
LABEL = "io.cerberus.audio8-sglang.runtime-fingerprint"


def active_container_fingerprints() -> set[str]:
    completed = subprocess.run(
        ["docker", "ps", "--format", f'{{{{.Label "{LABEL}"}}}}'],
        check=True,
        capture_output=True,
        text=True,
    )
    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def validate_private_cache(path: pathlib.Path, fingerprint: str) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
        raise ValueError(f"obsolete cache is not a real directory: {fingerprint}")
    if (info.st_uid, info.st_gid) != (os.getuid(), os.getgid()):
        raise ValueError(f"obsolete cache has the wrong owner: {fingerprint}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f"obsolete cache has the wrong mode: {fingerprint}")
    if path.resolve() != path:
        raise ValueError(f"obsolete cache path is not canonical: {fingerprint}")
    if os.path.ismount(path):
        raise ValueError(f"obsolete cache is a mount point: {fingerprint}")
    marker = path / ".runtime-fingerprint"
    marker_info = marker.lstat()
    if not stat.S_ISREG(marker_info.st_mode) or marker.is_symlink():
        raise ValueError(f"obsolete cache marker is unsafe: {fingerprint}")
    if (marker_info.st_uid, marker_info.st_gid) != (os.getuid(), os.getgid()):
        raise ValueError(f"obsolete cache marker has the wrong owner: {fingerprint}")
    if stat.S_IMODE(marker_info.st_mode) != 0o600:
        raise ValueError(f"obsolete cache marker has the wrong mode: {fingerprint}")
    if marker.read_text(encoding="ascii") != f"{fingerprint}\n":
        raise ValueError(f"obsolete cache marker does not match: {fingerprint}")


def prune_caches(
    cache_root: pathlib.Path,
    current_fingerprint: str,
    running_fingerprints: set[str],
    obsolete_fingerprints: frozenset[str] = KNOWN_OBSOLETE_FINGERPRINTS,
) -> list[str]:
    if cache_root.resolve() != cache_root or cache_root.is_symlink():
        raise ValueError("Audio8 cache root is not canonical")
    removed: list[str] = []
    for fingerprint in sorted(obsolete_fingerprints):
        if fingerprint == current_fingerprint or fingerprint in running_fingerprints:
            continue
        path = cache_root / fingerprint
        if not path.exists():
            continue
        validate_private_cache(path, fingerprint)
        shutil.rmtree(path)
        removed.append(fingerprint)
    return removed


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: prune_obsolete_caches.py LOCK ARTIFACT_ROOT")
    if os.geteuid() == 0:
        raise SystemExit("run cache pruning as the Audio8 service user, not root")
    identity = load_runtime_identity(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
    current = set(active_container_fingerprints())
    try:
        removed = prune_caches(
            CACHE_ROOT,
            identity.fingerprint,
            current,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"cannot prune obsolete Audio8 cache: {error}") from error
    for fingerprint in removed:
        print(f"Removed obsolete Audio8 cache {fingerprint}")


if __name__ == "__main__":
    main()
