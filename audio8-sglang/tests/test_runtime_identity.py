from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio8_runtime_identity", ROOT / "runtime_identity.py"
)
assert SPEC and SPEC.loader
runtime_identity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime_identity
SPEC.loader.exec_module(runtime_identity)


EXPECTED_REPOSITORY_FINGERPRINT = (
    "2cf49c3ec3433f53e46c016813885dbaedc212d2b7b93e872f1e87070008b7b4"
)


def fixture_lock() -> dict[str, object]:
    return {
        "base_image": f"example.invalid/runtime@sha256:{'1' * 64}",
        "sglang_omni_commit": "2" * 40,
        "audio8_tts_commit": "3" * 40,
        "image": "example/runtime:test",
        "model_directory": "model--revision",
        "model_revision": "4" * 40,
        "served_model_name": "example/model",
        "single_process_source_contract": {
            "second_sha256": "6" * 64,
            "first_sha256": "5" * 64,
        },
    }


def write_runtime_artifacts(root: pathlib.Path) -> None:
    (root / "Dockerfile").write_bytes(b"FROM example.invalid/runtime\n")
    (root / "check_health.py").write_bytes(b"health verifier\n")
    (root / "gateway.py").write_bytes(b"gateway\n")
    (root / "runtime_identity.py").write_bytes(b"identity verifier\n")
    (root / "verify_source_contract.py").write_bytes(b"source verifier\n")


class RuntimeIdentityTests(unittest.TestCase):
    def test_repository_fingerprint_is_deterministic(self) -> None:
        identity = runtime_identity.load_runtime_identity(
            ROOT / "RUNTIME.lock.json", ROOT
        )
        self.assertEqual(identity.fingerprint, EXPECTED_REPOSITORY_FINGERPRINT)
        self.assertEqual(set(identity.labels()), {
            runtime_identity.BASE_IMAGE_LABEL,
            runtime_identity.SGLANG_COMMIT_LABEL,
            runtime_identity.AUDIO8_COMMIT_LABEL,
            runtime_identity.FINGERPRINT_LABEL,
        })

    def test_json_order_does_not_change_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            patches = root / "patches"
            patches.mkdir()
            (patches / "b.patch").write_bytes(b"second\n")
            (patches / "a.patch").write_bytes(b"first\n")
            write_runtime_artifacts(root)
            lock_path = root / "lock.json"
            lock = fixture_lock()
            lock_path.write_text(json.dumps(lock), encoding="utf-8")
            first = runtime_identity.load_runtime_identity(lock_path, root)
            reversed_lock = dict(reversed(tuple(lock.items())))
            reversed_lock["single_process_source_contract"] = dict(
                reversed(
                    tuple(lock["single_process_source_contract"].items())  # type: ignore[union-attr]
                )
            )
            lock_path.write_text(json.dumps(reversed_lock), encoding="utf-8")
            second = runtime_identity.load_runtime_identity(lock_path, root)
            self.assertEqual(first.fingerprint, second.fingerprint)

    def test_patch_change_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            patches = root / "patches"
            patches.mkdir()
            patch = patches / "runtime.patch"
            patch.write_bytes(b"before\n")
            write_runtime_artifacts(root)
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(fixture_lock()), encoding="utf-8")
            before = runtime_identity.load_runtime_identity(lock_path, root)
            patch.write_bytes(b"after\n")
            after = runtime_identity.load_runtime_identity(lock_path, root)
            self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_dockerfile_change_changes_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            patches = root / "patches"
            patches.mkdir()
            (patches / "runtime.patch").write_bytes(b"patch\n")
            write_runtime_artifacts(root)
            lock_path = root / "lock.json"
            lock_path.write_text(json.dumps(fixture_lock()), encoding="utf-8")
            before = runtime_identity.load_runtime_identity(lock_path, root)
            (root / "Dockerfile").write_bytes(b"FROM changed.invalid/runtime\n")
            after = runtime_identity.load_runtime_identity(lock_path, root)
            self.assertNotEqual(before.fingerprint, after.fingerprint)

    def test_every_required_image_label_must_match(self) -> None:
        identity = runtime_identity.load_runtime_identity(
            ROOT / "RUNTIME.lock.json", ROOT
        )
        labels = {**identity.labels(), "inherited.extra": "allowed"}
        runtime_identity.verify_labels(identity, labels)
        for key in identity.labels():
            with self.subTest(key=key):
                invalid = dict(labels)
                invalid[key] = "wrong"
                with self.assertRaisesRegex(ValueError, key):
                    runtime_identity.verify_labels(identity, invalid)


if __name__ == "__main__":
    unittest.main()
