from __future__ import annotations

import importlib.util
import os
import pathlib
import stat
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio8_cache_validation", ROOT / "prepare_cache.py"
)
assert SPEC and SPEC.loader
cache_validation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cache_validation
SPEC.loader.exec_module(cache_validation)


FINGERPRINT = "a" * 64


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def secure_temporary_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(dir=pathlib.Path.home())


class CacheValidationTests(unittest.TestCase):
    def test_cache_is_private_marked_and_idempotent(self) -> None:
        with secure_temporary_directory() as temporary:
            root = pathlib.Path(temporary) / "cache" / FINGERPRINT
            for _ in range(2):
                cache_validation.prepare_cache(
                    str(root), FINGERPRINT, os.getuid(), os.getgid()
                )
            self.assertEqual(mode(root), 0o700)
            for child in cache_validation.CACHE_CHILDREN:
                path = root / child
                self.assertTrue(path.is_dir())
                self.assertFalse(path.is_symlink())
                self.assertEqual(mode(path), 0o700)
                self.assertEqual(path.resolve(), path)
            marker = root / cache_validation.MARKER_NAME
            self.assertEqual(mode(marker), 0o600)
            self.assertEqual(marker.read_text(encoding="ascii"), f"{FINGERPRINT}\n")

    def test_existing_marker_must_match_runtime(self) -> None:
        with secure_temporary_directory() as temporary:
            root = pathlib.Path(temporary) / "cache"
            cache_validation.prepare_cache(
                str(root), FINGERPRINT, os.getuid(), os.getgid()
            )
            with self.assertRaisesRegex(ValueError, "does not match"):
                cache_validation.prepare_cache(
                    str(root), "b" * 64, os.getuid(), os.getgid()
                )

    def test_preexisting_symlink_child_does_not_touch_target(self) -> None:
        with secure_temporary_directory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "cache"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            for child in cache_validation.CACHE_CHILDREN[:-1]:
                (root / child).mkdir(mode=0o700)
            target = base / "target"
            target.mkdir(mode=0o751)
            target.chmod(0o751)
            (root / cache_validation.CACHE_CHILDREN[-1]).symlink_to(
                target, target_is_directory=True
            )
            before = target.stat()

            with self.assertRaisesRegex(ValueError, "real private directory"):
                cache_validation.prepare_cache(
                    str(root), FINGERPRINT, os.getuid(), os.getgid()
                )

            after = target.stat()
            self.assertEqual(stat.S_IMODE(after.st_mode), 0o751)
            self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))
            self.assertFalse((root / cache_validation.MARKER_NAME).exists())

    def test_symlink_cache_root_does_not_touch_target(self) -> None:
        with secure_temporary_directory() as temporary:
            base = pathlib.Path(temporary)
            target = base / "target"
            target.mkdir(mode=0o751)
            target.chmod(0o751)
            alias = base / "cache"
            alias.symlink_to(target, target_is_directory=True)

            with self.assertRaises(OSError):
                cache_validation.prepare_cache(
                    str(alias), FINGERPRINT, os.getuid(), os.getgid()
                )

            self.assertEqual(mode(target), 0o751)
            self.assertEqual(list(target.iterdir()), [])

    def test_symlink_marker_does_not_touch_target(self) -> None:
        with secure_temporary_directory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "cache"
            root.mkdir(mode=0o700)
            root.chmod(0o700)
            for child in cache_validation.CACHE_CHILDREN:
                (root / child).mkdir(mode=0o700)
            target = base / "target"
            target.write_text("unchanged\n", encoding="ascii")
            target.chmod(0o644)
            (root / cache_validation.MARKER_NAME).symlink_to(target)

            with self.assertRaisesRegex(ValueError, "must not be a symlink"):
                cache_validation.prepare_cache(
                    str(root), FINGERPRINT, os.getuid(), os.getgid()
                )

            self.assertEqual(target.read_text(encoding="ascii"), "unchanged\n")
            self.assertEqual(mode(target), 0o644)

    def test_existing_root_and_children_require_exact_private_modes(self) -> None:
        with secure_temporary_directory() as temporary:
            base = pathlib.Path(temporary)
            root = base / "cache"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "cache root must have mode 0700"):
                cache_validation.prepare_cache(
                    str(root), FINGERPRINT, os.getuid(), os.getgid()
                )
            self.assertEqual(mode(root), 0o755)

            root.chmod(0o700)
            child = root / cache_validation.CACHE_CHILDREN[0]
            child.mkdir(mode=0o755)
            child.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "must have mode 0700"):
                cache_validation.prepare_cache(
                    str(root), FINGERPRINT, os.getuid(), os.getgid()
                )
            self.assertEqual(mode(child), 0o755)

    def test_unmarked_populated_cache_is_not_adopted(self) -> None:
        for unexpected_root_entry in (False, True):
            with self.subTest(unexpected_root_entry=unexpected_root_entry):
                with secure_temporary_directory() as temporary:
                    root = pathlib.Path(temporary) / "cache"
                    root.mkdir(mode=0o700)
                    root.chmod(0o700)
                    child = root / cache_validation.CACHE_CHILDREN[0]
                    child.mkdir(mode=0o700)
                    if unexpected_root_entry:
                        (root / "foreign-entry").write_text(
                            "do not adopt\n", encoding="ascii"
                        )
                        expected_error = "unexpected entries"
                    else:
                        (child / "foreign-entry").write_text(
                            "do not adopt\n", encoding="ascii"
                        )
                        expected_error = "must be empty"

                    with self.assertRaisesRegex(ValueError, expected_error):
                        cache_validation.prepare_cache(
                            str(root), FINGERPRINT, os.getuid(), os.getgid()
                        )

                    self.assertFalse(
                        (root / cache_validation.MARKER_NAME).exists()
                    )
                    self.assertEqual(
                        (root / "foreign-entry" if unexpected_root_entry else child / "foreign-entry").read_text(
                            encoding="ascii"
                        ),
                        "do not adopt\n",
                    )

    def test_world_writable_intermediate_parent_is_rejected(self) -> None:
        with secure_temporary_directory() as temporary:
            intermediate = pathlib.Path(temporary) / "writable"
            intermediate.mkdir(mode=0o777)
            intermediate.chmod(0o777)
            root = intermediate / "cache"

            with self.assertRaisesRegex(ValueError, "group- or world-writable"):
                cache_validation.prepare_cache(
                    str(root), FINGERPRINT, os.getuid(), os.getgid()
                )

            self.assertEqual(mode(intermediate), 0o777)
            self.assertFalse(root.exists())

    def test_noncanonical_cache_path_is_rejected(self) -> None:
        with secure_temporary_directory() as temporary:
            path = f"{temporary}/missing/../cache"
            with self.assertRaisesRegex(ValueError, "normalized absolute path"):
                cache_validation.prepare_cache(
                    path, FINGERPRINT, os.getuid(), os.getgid()
                )


if __name__ == "__main__":
    unittest.main()
