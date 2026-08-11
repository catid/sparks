from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audio8_source_contract", ROOT / "verify_source_contract.py"
)
assert SPEC and SPEC.loader
contract = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = contract
SPEC.loader.exec_module(contract)


class SourceContractTests(unittest.TestCase):
    def make_sources(self, root: pathlib.Path) -> tuple[pathlib.Path, ...]:
        sglang = root / "sglang"
        audio8 = root / "audio8"
        launcher = sglang / "sglang_omni/serve/launcher.py"
        compiler = sglang / "sglang_omni/config/compiler.py"
        config = audio8 / "sglang_omni/configs/audio8_tts_0_6b.yaml"
        for path in (launcher, compiler, config):
            path.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text(
            "need_multi_process = len(gpu_ids) > 1\n"
            "runner = build_pipeline_runner(pipeline_config)\n"
            "app = create_app(client, model_name=model_name)\n",
            encoding="utf-8",
        )
        compiler.write_text(
            "executor = factory(**stage_cfg.executor.args)\n", encoding="utf-8"
        )
        config.write_text("device: cuda:0\ndevice: cuda:0\ndevice: cuda:0\n")
        lock = root / "lock.json"
        hashes = {
            key: hashlib.sha256(
                (sglang if owner == "sglang" else audio8)
                .joinpath(relative)
                .read_bytes()
            ).hexdigest()
            for key, (owner, relative) in contract.CONTRACT_FILES.items()
        }
        lock.write_text(
            json.dumps({"single_process_source_contract": hashes}),
            encoding="utf-8",
        )
        return lock, sglang, audio8, config

    def test_reviewed_single_process_contract_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock, sglang, audio8, _config = self.make_sources(
                pathlib.Path(temporary)
            )
            contract.verify_source_contract(lock, sglang, audio8)

    def test_multi_process_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            lock, sglang, audio8, config = self.make_sources(root)
            config.write_text(
                config.read_text(encoding="utf-8") + "gpu_placement: {}\n",
                encoding="utf-8",
            )
            data = json.loads(lock.read_text(encoding="utf-8"))
            data["single_process_source_contract"]["audio8_config_sha256"] = (
                hashlib.sha256(config.read_bytes()).hexdigest()
            )
            lock.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "multi-process"):
                contract.verify_source_contract(lock, sglang, audio8)


if __name__ == "__main__":
    unittest.main()
