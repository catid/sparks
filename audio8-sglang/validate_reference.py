#!/usr/bin/env python3
"""Validate the private fixed-reference mount without reading its contents."""

from __future__ import annotations

import os
import pathlib
import stat
import sys


def validate_reference_directory(path_text: str, owner_uid: int, owner_gid: int) -> None:
    if owner_uid < 0 or owner_gid < 0:
        raise ValueError("reference owner IDs must be non-negative")
    if not path_text.startswith("/") or path_text != os.path.realpath(path_text):
        raise ValueError("reference directory must use its canonical absolute path")
    directory = pathlib.Path(path_text)
    entries = (
        (
            directory,
            "directory",
            stat.S_ISDIR,
            stat.S_IRUSR | stat.S_IXUSR,
            False,
        ),
        (
            directory / "reference.wav",
            "reference audio",
            stat.S_ISREG,
            stat.S_IRUSR,
            True,
        ),
        (
            directory / "transcript.txt",
            "reference transcript",
            stat.S_ISREG,
            stat.S_IRUSR,
            True,
        ),
    )
    for path, label, expected_type, required_owner_bits, must_be_nonempty in entries:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not expected_type(info.st_mode):
            raise ValueError(f"{label} has the wrong type or is a symlink")
        if info.st_uid != owner_uid or info.st_gid != owner_gid:
            raise ValueError(f"{label} must be owned by the service user and group")
        mode = stat.S_IMODE(info.st_mode)
        if mode & 0o077:
            raise ValueError(f"{label} must have no group or other permissions")
        if mode & required_owner_bits != required_owner_bits:
            raise ValueError(f"{label} is not readable by the service user")
        if must_be_nonempty and info.st_size <= 0:
            raise ValueError(f"{label} must not be empty")


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: validate_reference.py PATH UID GID")
    try:
        validate_reference_directory(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
    except (OSError, ValueError) as error:
        raise SystemExit(f"invalid private Audio8 reference: {error}") from error


if __name__ == "__main__":
    main()
