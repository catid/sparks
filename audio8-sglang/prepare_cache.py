#!/usr/bin/env python3
"""Create and validate a private, runtime-keyed executable cache."""

from __future__ import annotations

import os
import pathlib
import re
import stat
import sys


CACHE_CHILDREN = (
    "cuda",
    "flashinfer",
    "huggingface",
    "torchinductor",
    "triton",
)
MARKER_NAME = ".runtime-fingerprint"
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def validate_directory(
    descriptor: int, label: str, owner_uid: int, owner_gid: int
) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} is not a directory")
    if info.st_uid != owner_uid or info.st_gid != owner_gid:
        raise ValueError(f"{label} must be owned by the service user and group")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError(f"{label} must have mode 0700")


def validate_intermediate_parent(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("cache path parent is not a directory")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError(
            "cache path parents must not be group- or world-writable"
        )


def open_directory_component(parent: int, name: str, *, create: bool) -> int:
    try:
        return os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        created = True
    except FileExistsError:
        pass
    descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    if created:
        os.fchmod(descriptor, 0o700)
    return descriptor


def open_cache_root(path_text: str) -> int:
    if (
        not path_text.startswith("/")
        or path_text == "/"
        or path_text != os.path.normpath(path_text)
    ):
        raise ValueError("cache root must use a normalized absolute path")
    components = pathlib.PurePosixPath(path_text).parts[1:]
    descriptor = os.open("/", DIRECTORY_FLAGS)
    try:
        for component in components:
            validate_intermediate_parent(descriptor)
            next_descriptor = open_directory_component(
                descriptor, component, create=True
            )
            os.close(descriptor)
            descriptor = next_descriptor
        if os.path.realpath(path_text) != path_text:
            raise ValueError("cache root must use its canonical no-symlink path")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def marker_exists(root_descriptor: int) -> bool:
    try:
        os.stat(MARKER_NAME, dir_fd=root_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def validate_existing_marker(
    root_descriptor: int, fingerprint: str, owner_uid: int, owner_gid: int
) -> None:
    expected = f"{fingerprint}\n".encode("ascii")
    try:
        descriptor = os.open(MARKER_NAME, FILE_FLAGS, dir_fd=root_descriptor)
    except OSError as error:
        raise ValueError(
            "cache fingerprint marker must exist and must not be a symlink"
        ) from error

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("cache fingerprint marker must be a regular file")
        if info.st_uid != owner_uid or info.st_gid != owner_gid:
            raise ValueError(
                "cache fingerprint marker must be owned by the service user and group"
            )
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("cache fingerprint marker must have mode 0600")
        contents = os.read(descriptor, len(expected) + 1)
        if contents != expected:
            raise ValueError("cache fingerprint marker does not match this runtime")
    finally:
        os.close(descriptor)


def create_marker(root_descriptor: int, fingerprint: str) -> None:
    expected = f"{fingerprint}\n".encode("ascii")
    try:
        descriptor = os.open(
            MARKER_NAME,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
    except OSError as error:
        raise ValueError("cannot create cache fingerprint marker") from error
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(expected):
            written = os.write(descriptor, expected[offset:])
            if written <= 0:
                raise ValueError("cannot write cache fingerprint marker")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_unmarked_cache(
    root_descriptor: int,
    owner_uid: int,
    owner_gid: int,
    *,
    require_all_children: bool,
) -> None:
    entries = set(os.listdir(root_descriptor))
    allowed = set(CACHE_CHILDREN)
    unexpected = entries - allowed
    if unexpected:
        raise ValueError("unmarked cache root contains unexpected entries")
    if require_all_children and entries != allowed:
        raise ValueError("unmarked cache root changed during initialization")
    for child in sorted(entries):
        try:
            descriptor = os.open(child, DIRECTORY_FLAGS, dir_fd=root_descriptor)
        except OSError as error:
            raise ValueError(
                f"cache child must be a real private directory: {child}"
            ) from error
        try:
            validate_directory(
                descriptor, f"cache child {child}", owner_uid, owner_gid
            )
            if os.listdir(descriptor):
                raise ValueError(
                    f"unmarked cache child must be empty: {child}"
                )
        finally:
            os.close(descriptor)


def prepare_cache(
    path_text: str, fingerprint: str, owner_uid: int, owner_gid: int
) -> None:
    if owner_uid < 0 or owner_gid < 0:
        raise ValueError("cache owner IDs must be non-negative")
    if FINGERPRINT.fullmatch(fingerprint) is None:
        raise ValueError("cache runtime fingerprint must be lowercase sha256")
    root_descriptor = open_cache_root(path_text)
    try:
        validate_directory(root_descriptor, "cache root", owner_uid, owner_gid)
        marked = marker_exists(root_descriptor)
        if marked:
            validate_existing_marker(
                root_descriptor, fingerprint, owner_uid, owner_gid
            )
        else:
            validate_unmarked_cache(
                root_descriptor,
                owner_uid,
                owner_gid,
                require_all_children=False,
            )
        for child in CACHE_CHILDREN:
            try:
                descriptor = open_directory_component(
                    root_descriptor, child, create=True
                )
            except OSError as error:
                raise ValueError(
                    f"cache child must be a real private directory: {child}"
                ) from error
            try:
                validate_directory(
                    descriptor, f"cache child {child}", owner_uid, owner_gid
                )
                expected_path = os.path.join(path_text, child)
                if os.path.realpath(expected_path) != expected_path:
                    raise ValueError(
                        f"cache child must remain inside the cache root: {child}"
                    )
            finally:
                os.close(descriptor)
        if not marked:
            validate_unmarked_cache(
                root_descriptor,
                owner_uid,
                owner_gid,
                require_all_children=True,
            )
            create_marker(root_descriptor, fingerprint)
        os.fsync(root_descriptor)
    finally:
        os.close(root_descriptor)


def main() -> None:
    if len(sys.argv) != 5:
        raise SystemExit("usage: prepare_cache.py PATH FINGERPRINT UID GID")
    try:
        prepare_cache(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
    except (OSError, ValueError) as error:
        raise SystemExit(f"invalid Audio8 SGLang executable cache: {error}") from error


if __name__ == "__main__":
    main()
