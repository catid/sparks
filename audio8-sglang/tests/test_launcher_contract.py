from __future__ import annotations

import os
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LauncherContractTests(unittest.TestCase):
    def test_provenance_cache_and_loopback_contracts_are_wired(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        build = (ROOT / "build-image.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "run-backend.sh").read_text(encoding="utf-8")
        for label in (
            "io.cerberus.audio8-sglang.base-image",
            "io.cerberus.audio8-sglang.sglang-omni-commit",
            "io.cerberus.audio8-sglang.audio8-tts-commit",
            "io.cerberus.audio8-sglang.source-contract-patchset-sha256",
        ):
            self.assertIn(label, dockerfile)
        self.assertIn("AUDIO8_RUNTIME_FINGERPRINT=${values[7]}", build)
        self.assertIn("runtime_identity.py\" verify-labels", build)
        self.assertIn("runtime_identity.py\" verify-labels", launcher)
        self.assertIn("prepare_cache.py", launcher)
        self.assertIn("/${runtime_fingerprint}}", launcher)
        self.assertIn('--publish "127.0.0.1:${backend_port}:8010"', launcher)

    def test_experimental_backend_rejects_every_nontrial_port(self) -> None:
        for port in ("1", "8010", "8020", "18011", "65535"):
            environment = dict(os.environ)
            environment.update(
                AUDIO8_SGLANG_EXPERIMENTAL="1",
                AUDIO8_SGLANG_BACKEND_PORT=port,
            )
            completed = subprocess.run(
                [str(ROOT / "run-backend.sh")],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            with self.subTest(port=port):
                self.assertEqual(completed.returncode, 2)
                self.assertIn("locked to 18010", completed.stderr)


if __name__ == "__main__":
    unittest.main()
