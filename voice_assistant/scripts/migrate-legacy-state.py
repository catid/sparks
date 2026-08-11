#!/usr/bin/env python3
"""Privately migrate the pre-Cerberus voice state into canonical paths.

The utility deliberately emits no file contents. It copies legacy root-owned
secret and override files only when their canonical targets are absent, copies
user-owned trees only when the canonical tree is absent or empty, and performs
the known identity/path rewrites in copied OpenClaw configuration and workspace
instructions.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any


class MigrationError(RuntimeError):
    """A legacy object was unsafe or conflicted with canonical state."""


def absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path == Path("/") or ".." in path.parts:
        raise argparse.ArgumentTypeError("migration paths must be safe absolute paths")
    return path


def optional_stat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def require_directory(path: Path, *, uid: int, gid: int) -> os.stat_result:
    info = optional_stat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MigrationError(f"expected a regular directory: {path}")
    if info.st_uid != uid or info.st_gid != gid:
        raise MigrationError(f"unexpected directory ownership: {path}")
    return info


def require_regular_file(
    path: Path, *, uid: int, gid: int, mode: int | None = None
) -> os.stat_result:
    info = optional_stat(path)
    if info is None or not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MigrationError(f"expected a regular file: {path}")
    if info.st_uid != uid or info.st_gid != gid:
        raise MigrationError(f"unexpected file ownership: {path}")
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise MigrationError(f"unexpected file mode: {path}")
    return info


def ensure_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    info = optional_stat(path)
    if info is None or not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise MigrationError(f"unsafe destination directory: {path}")
    os.chown(path, uid, gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def copy_private_file_if_absent(
    source: Path,
    destination: Path,
    *,
    uid: int,
    gid: int,
    create_private_parent: bool = True,
) -> bool:
    source_info = optional_stat(source)
    if source_info is None:
        if optional_stat(destination) is not None:
            require_regular_file(destination, uid=uid, gid=gid, mode=0o600)
        return False
    require_regular_file(source, uid=uid, gid=gid, mode=0o600)

    destination_info = optional_stat(destination)
    if destination_info is not None:
        require_regular_file(destination, uid=uid, gid=gid, mode=0o600)
        return False

    if create_private_parent:
        ensure_directory(destination.parent, uid=uid, gid=gid, mode=0o700)
    else:
        # `/etc/default` is shared host configuration. Validate it, but never
        # chown or chmod it while migrating a private file within it.
        require_directory(destination.parent, uid=uid, gid=gid)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.migration.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            source_open_info = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(source_open_info.st_mode)
                or source_open_info.st_uid != uid
                or source_open_info.st_gid != gid
                or stat.S_IMODE(source_open_info.st_mode) != 0o600
            ):
                raise MigrationError(f"legacy private file changed during copy: {source}")
            with os.fdopen(source_descriptor, "rb", closefd=False) as source_file:
                with os.fdopen(descriptor, "wb", closefd=False) as destination_file:
                    shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
                    destination_file.flush()
                    os.fsync(descriptor)
        finally:
            os.close(source_descriptor)
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def validate_tree(path: Path, *, uid: int, gid: int) -> None:
    require_directory(path, uid=uid, gid=gid)
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in (*directories, *files):
            candidate = root_path / name
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode):
                continue
            if not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise MigrationError(f"unsupported object in private state: {candidate}")
            if info.st_uid != uid or info.st_gid != gid:
                raise MigrationError(f"unexpected private-state ownership: {candidate}")


def chown_tree(path: Path, *, uid: int, gid: int) -> None:
    os.chown(path, uid, gid, follow_symlinks=False)
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in (*directories, *files):
            os.chown(root_path / name, uid, gid, follow_symlinks=False)


def copy_tree_if_absent(
    source: Path, destination: Path, *, uid: int, gid: int
) -> bool:
    if optional_stat(source) is None:
        if optional_stat(destination) is not None:
            require_directory(destination, uid=uid, gid=gid)
        return False
    validate_tree(source, uid=uid, gid=gid)

    destination_info = optional_stat(destination)
    if destination_info is not None:
        require_directory(destination, uid=uid, gid=gid)
        if any(destination.iterdir()):
            return False
        destination.rmdir()

    ensure_directory(destination.parent, uid=uid, gid=gid, mode=0o700)
    stage = destination.parent / f".{destination.name}.migration.{os.getpid()}"
    if optional_stat(stage) is not None:
        raise MigrationError(f"migration staging path already exists: {stage}")
    try:
        shutil.copytree(source, stage, symlinks=True, copy_function=shutil.copy2)
        chown_tree(stage, uid=uid, gid=gid)
        validate_tree(stage, uid=uid, gid=gid)
        os.replace(stage, destination)
        return True
    finally:
        if optional_stat(stage) is not None:
            shutil.rmtree(stage)


def rewrite_identity(value: Any, replacements: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        for old, new in replacements:
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [rewrite_identity(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            key: rewrite_identity(item, replacements) for key, item in value.items()
        }
    return value


def atomic_write_text(
    path: Path, text: str, *, uid: int, gid: int, mode: int
) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.identity.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        encoded = text.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def canonicalize_openclaw_files(
    *,
    state: Path,
    workspace: Path,
    legacy_state: Path,
    legacy_workspace: Path,
    legacy_cache: Path,
    cache: Path,
    uid: int,
    gid: int,
) -> None:
    replacements = (
        (str(legacy_state), str(state)),
        (str(legacy_workspace), str(workspace)),
        (str(legacy_cache), str(cache)),
        ("Cerebrus", "Cerberus"),
        ("cerebrus", "cerberus"),
    )
    config = state / "openclaw.json"
    if optional_stat(config) is not None:
        info = require_regular_file(config, uid=uid, gid=gid)
        data = json.loads(config.read_text(encoding="utf-8"))
        rewritten = rewrite_identity(data, replacements)
        try:
            provider = rewritten["models"]["providers"]["vllm"]
        except (KeyError, TypeError):
            provider = None
        if isinstance(provider, dict) and provider.get("baseUrl") in {
            "http://cerberus1:8889/v1",
            "http://cerebrus1:8889/v1",
        }:
            provider["baseUrl"] = "http://cerberus1.local:8889/v1"
        if rewritten != data:
            atomic_write_text(
                config,
                json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n",
                uid=uid,
                gid=gid,
                mode=0o600,
            )
        elif stat.S_IMODE(info.st_mode) != 0o600:
            os.chmod(config, 0o600, follow_symlinks=False)

    agents = workspace / "AGENTS.md"
    if optional_stat(agents) is not None:
        info = require_regular_file(agents, uid=uid, gid=gid)
        original = agents.read_text(encoding="utf-8")
        rewritten = original
        for old, new in replacements:
            rewritten = rewritten.replace(old, new)
        if rewritten != original:
            atomic_write_text(
                agents,
                rewritten,
                uid=uid,
                gid=gid,
                mode=stat.S_IMODE(info.st_mode),
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--legacy-secret", required=True, type=absolute_path)
    result.add_argument("--secret", required=True, type=absolute_path)
    result.add_argument("--legacy-asr-env", required=True, type=absolute_path)
    result.add_argument("--asr-env", required=True, type=absolute_path)
    result.add_argument("--legacy-bridge-env", required=True, type=absolute_path)
    result.add_argument("--bridge-env", required=True, type=absolute_path)
    result.add_argument("--legacy-state", required=True, type=absolute_path)
    result.add_argument("--state", required=True, type=absolute_path)
    result.add_argument("--legacy-workspace", required=True, type=absolute_path)
    result.add_argument("--workspace", required=True, type=absolute_path)
    result.add_argument("--legacy-cache", required=True, type=absolute_path)
    result.add_argument("--cache", required=True, type=absolute_path)
    result.add_argument("--legacy-asr-cache", required=True, type=absolute_path)
    result.add_argument("--asr-cache", required=True, type=absolute_path)
    result.add_argument("--service-uid", required=True, type=int)
    result.add_argument("--service-gid", required=True, type=int)
    result.add_argument("--secret-uid", type=int, default=0)
    result.add_argument("--secret-gid", type=int, default=0)
    return result


def main() -> None:
    arguments = parser().parse_args()
    copy_private_file_if_absent(
        arguments.legacy_secret,
        arguments.secret,
        uid=arguments.secret_uid,
        gid=arguments.secret_gid,
    )
    for source, destination in (
        (arguments.legacy_asr_env, arguments.asr_env),
        (arguments.legacy_bridge_env, arguments.bridge_env),
    ):
        copy_private_file_if_absent(
            source,
            destination,
            uid=arguments.secret_uid,
            gid=arguments.secret_gid,
            create_private_parent=False,
        )
    for source, destination in (
        (arguments.legacy_state, arguments.state),
        (arguments.legacy_workspace, arguments.workspace),
        (arguments.legacy_cache, arguments.cache),
        (arguments.legacy_asr_cache, arguments.asr_cache),
    ):
        copy_tree_if_absent(
            source,
            destination,
            uid=arguments.service_uid,
            gid=arguments.service_gid,
        )
    canonicalize_openclaw_files(
        state=arguments.state,
        workspace=arguments.workspace,
        legacy_state=arguments.legacy_state,
        legacy_workspace=arguments.legacy_workspace,
        legacy_cache=arguments.legacy_cache,
        cache=arguments.cache,
        uid=arguments.service_uid,
        gid=arguments.service_gid,
    )
    print("Legacy Cerberus private-state migration checked; no contents were emitted.")


if __name__ == "__main__":
    try:
        main()
    except (MigrationError, OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Cerberus private-state migration failed: {error}") from error
