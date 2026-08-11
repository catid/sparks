from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio8_reference_validation", ROOT / "validate_reference.py"
)
assert SPEC and SPEC.loader
validation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validation
SPEC.loader.exec_module(validation)


class ReferenceValidationTests(unittest.TestCase):
    def make_reference(self, root: pathlib.Path) -> pathlib.Path:
        reference = root / "authorized-voice"
        reference.mkdir(mode=0o700)
        audio = reference / "reference.wav"
        transcript = reference / "transcript.txt"
        audio.write_bytes(b"RIFF-private-test-fixture")
        transcript.write_text("Authorized test transcript.", encoding="utf-8")
        audio.chmod(0o600)
        transcript.chmod(0o600)
        reference.chmod(0o700)
        return reference

    def test_private_canonical_reference_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = self.make_reference(pathlib.Path(temporary))
            validation.validate_reference_directory(
                str(reference), os.getuid(), os.getgid()
            )

    def test_group_or_other_permissions_are_rejected(self) -> None:
        for relative_path, mode in (
            (".", 0o710),
            ("reference.wav", 0o640),
            ("transcript.txt", 0o604),
        ):
            with (
                self.subTest(path=relative_path),
                tempfile.TemporaryDirectory() as temporary,
            ):
                reference = self.make_reference(pathlib.Path(temporary))
                (reference / relative_path).chmod(mode)
                with self.assertRaisesRegex(ValueError, "group or other"):
                    validation.validate_reference_directory(
                        str(reference), os.getuid(), os.getgid()
                    )

    def test_wrong_owner_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = self.make_reference(pathlib.Path(temporary))
            with self.assertRaisesRegex(ValueError, "owned"):
                validation.validate_reference_directory(
                    str(reference), os.getuid() + 1, os.getgid()
                )

    def test_noncanonical_symlink_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            reference = self.make_reference(root)
            alias = root / "voice-alias"
            alias.symlink_to(reference, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "canonical"):
                validation.validate_reference_directory(
                    str(alias), os.getuid(), os.getgid()
                )

    def test_symlinked_reference_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reference = self.make_reference(pathlib.Path(temporary))
            audio = reference / "reference.wav"
            target = reference / "audio-target.wav"
            audio.rename(target)
            audio.symlink_to(target.name)
            with self.assertRaisesRegex(ValueError, "symlink"):
                validation.validate_reference_directory(
                    str(reference), os.getuid(), os.getgid()
                )


if __name__ == "__main__":
    unittest.main()
