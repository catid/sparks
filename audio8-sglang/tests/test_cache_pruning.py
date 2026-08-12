from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SPEC = importlib.util.spec_from_file_location(
    "audio8_cache_pruning", ROOT / "prune_obsolete_caches.py"
)
assert SPEC and SPEC.loader
pruning = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pruning
SPEC.loader.exec_module(pruning)


class CachePruningTests(unittest.TestCase):
    def make_cache(self, root: pathlib.Path, fingerprint: str) -> pathlib.Path:
        cache = root / fingerprint
        cache.mkdir(mode=0o700)
        cache.chmod(0o700)
        marker = cache / ".runtime-fingerprint"
        marker.write_text(f"{fingerprint}\n", encoding="ascii")
        marker.chmod(0o600)
        (cache / "compiled.bin").write_bytes(b"cache")
        return cache

    def test_only_known_inactive_obsolete_cache_is_removed(self) -> None:
        old, current = tuple(sorted(pruning.KNOWN_OBSOLETE_FINGERPRINTS))
        unknown = "f" * 64
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            root = pathlib.Path(temporary).resolve()
            root.chmod(0o700)
            self.make_cache(root, old)
            self.make_cache(root, current)
            self.make_cache(root, unknown)
            removed = pruning.prune_caches(root, current, set())
            self.assertEqual(removed, [old])
            self.assertFalse((root / old).exists())
            self.assertTrue((root / current).exists())
            self.assertTrue((root / unknown).exists())

    def test_running_cache_is_preserved(self) -> None:
        old = next(iter(pruning.KNOWN_OBSOLETE_FINGERPRINTS))
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            root = pathlib.Path(temporary).resolve()
            root.chmod(0o700)
            self.make_cache(root, old)
            self.assertEqual(pruning.prune_caches(root, "e" * 64, {old}), [])
            self.assertTrue((root / old).exists())

    def test_mismatched_marker_fails_closed(self) -> None:
        old = next(iter(pruning.KNOWN_OBSOLETE_FINGERPRINTS))
        with tempfile.TemporaryDirectory(dir=pathlib.Path.home()) as temporary:
            root = pathlib.Path(temporary).resolve()
            root.chmod(0o700)
            cache = self.make_cache(root, old)
            (cache / ".runtime-fingerprint").write_text("wrong\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "does not match"):
                pruning.prune_caches(root, "e" * 64, set())
            self.assertTrue(cache.exists())


if __name__ == "__main__":
    unittest.main()
