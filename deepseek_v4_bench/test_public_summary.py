#!/usr/bin/env python3
"""Network-free tests for the MIA3 benchmark profile and public projection."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import make_public_summary


HERE = Path(__file__).resolve().parent


def private_source() -> dict[str, object]:
    secret = "DO-NOT-PUBLISH-PRIVATE-VALUE"
    result_row = {
        "label": secret,
        "batch_size": 1,
        "repeats": 3,
        "requests": 3,
        "successful": 3,
        "batch_wall_s_total": 60.0,
        "reported_prompt_tokens_total": 3072,
        "completion_tokens_total": 3072,
        "aggregate_prompt_tokens_per_s": 51.2,
        "aggregate_output_tokens_per_s": 51.2,
        "aggregate_output_tokens_per_s_per_user": 51.2,
        "request_output_tokens_per_s_after_first_mean": 54.0,
        "request_output_tokens_per_s_after_first_p50": 54.0,
        "request_output_tokens_per_s_after_first_p95": 55.0,
        "request_output_tokens_per_s_e2e_mean": 51.2,
        "ttft_mean_s": 1.2,
        "ttft_p50_s": 1.1,
        "ttft_p95_s": 1.4,
        "e2e_p50_s": 20.0,
        "e2e_p95_s": 21.0,
        "prompt_tokens_min": 1024,
        "prompt_tokens_max": 1024,
        "completion_tokens_min": 1024,
        "completion_tokens_max": 1024,
        "finish_reason_counts": {"length": 3, secret: 2},
        "arbitrary_private_field": secret,
    }
    wave_row = {
        "label": secret,
        "batch_size": 1,
        "repeat": 1,
        "requests": 1,
        "successful": 1,
        "batch_wall_s": 20.0,
        "reported_prompt_tokens_total": 1024,
        "completion_tokens_total": 1024,
        "aggregate_prompt_tokens_per_s": 51.2,
        "aggregate_output_tokens_per_s": 51.2,
        "finish_reason_counts": {"length": 1},
    }
    return {
        "status": "completed",
        "updated_at": "2026-08-10T00:00:00Z",
        "configuration": {
            "label": secret,
            "endpoint": f"http://{secret}:8893",
            "model": f"/private/{secret}/model",
            "concurrency": [1],
            "repeats": 3,
            "target_prompt_tokens": 1024,
            "prompt_tolerance": 12,
            "max_output_tokens": 1024,
            "min_output_tokens": 1024,
            "ignore_eos": True,
            "reasoning_effort": "max",
            "temperature": 1.0,
            "top_p": 1.0,
            "thinking": True,
            "warmup_output_tokens": 128,
            "seed": 260810,
            "tool_execution": "disabled; calls are recorded only",
        },
        "prompt_calibration": [
            {
                "case_index": 0,
                "token_count": 1024,
                "pad_words": 100,
                "prompt": secret,
            }
        ],
        "waves": [wave_row],
        "by_concurrency": [result_row],
        "metrics": {
            "before_error": secret,
            "after_error": secret,
            "delta": {
                "vllm:spec_decode_num_drafts_total": 100.0,
                secret: 999.0,
            },
            "accepted_tokens_per_draft_step": 3.5,
            "draft_token_acceptance_percent": 70.0,
        },
        "private_requests": [{"reasoning": secret, "request_id": secret}],
    }


class PublicProjectionTests(unittest.TestCase):
    def test_projection_excludes_private_and_unknown_fields(self) -> None:
        source = private_source()
        digest = hashlib.sha256(b"private summary").hexdigest()
        result = make_public_summary.build_public_summary(
            source, "pp3-target-auto", digest
        )
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn("DO-NOT-PUBLISH", encoded)
        self.assertNotIn("endpoint", encoded)
        self.assertNotIn("model", encoded)
        self.assertNotIn('"reasoning":', encoded)
        self.assertEqual(result["label"], "pp3-target-auto")
        self.assertEqual(
            result["by_concurrency"][0]["finish_reason_counts"],
            {"length": 3, "other": 2},
        )
        self.assertEqual(
            result["speculative_decoding"]["counter_delta"],
            {"vllm:spec_decode_num_drafts_total": 100.0},
        )

    def test_projection_rejects_path_like_public_label(self) -> None:
        with self.assertRaises(ValueError):
            make_public_summary.build_public_summary(
                private_source(),
                "../../private",
                hashlib.sha256(b"private summary").hexdigest(),
            )

    def test_cli_writes_redacted_json_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "summary.json"
            json_out = temp / "summary.public.json"
            csv_out = temp / "summary.public.csv"
            source.write_text(json.dumps(private_source()), encoding="utf-8")
            subprocess.run(
                [
                    sys.executable,
                    str(HERE / "make_public_summary.py"),
                    "--source",
                    str(source),
                    "--json-out",
                    str(json_out),
                    "--csv-out",
                    str(csv_out),
                    "--label",
                    "pp3-target-auto",
                ],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotIn("DO-NOT-PUBLISH", json_out.read_text())
            self.assertNotIn("DO-NOT-PUBLISH", csv_out.read_text())
            self.assertIn("aggregate_output_tokens_per_s", csv_out.read_text())

    def test_cli_refuses_to_overwrite_private_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "summary.json"
            source.write_text(json.dumps(private_source()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(HERE / "make_public_summary.py"),
                    "--source",
                    str(source),
                    "--json-out",
                    str(source),
                    "--csv-out",
                    str(temp / "summary.csv"),
                    "--label",
                    "pp3-target-auto",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("DO-NOT-PUBLISH", source.read_text())


class Mia3RunnerTests(unittest.TestCase):
    def test_dry_run_has_fixed_apples_to_apples_matrix(self) -> None:
        runner = HERE / "run_mia3_fixed1024.sh"
        environment = dict(os.environ)
        environment["BENCH_PYTHON"] = sys.executable
        completed = subprocess.run(
            [str(runner), "--label", "unit-test", "--dry-run"],
            cwd=HERE.parent,
            env=environment,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["concurrency"], [1, 2, 4, 8])
        self.assertEqual(payload["repeats"], 3)
        self.assertEqual(payload["target_prompt_tokens"], 1024)
        self.assertEqual(payload["output_tokens"], 1024)
        self.assertEqual(payload["warmup_output_tokens"], 128)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertTrue(payload["force_exact_length"])
        self.assertEqual(payload["files_written"], 0)

    def test_runner_log_does_not_break_empty_output_directory(self) -> None:
        runner = HERE / "run_mia3_fixed1024.sh"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            artifact_root = temp / "artifacts"
            fake_python = temp / "fake-python"
            fake_python.write_text(
                r"""#!/usr/bin/env python3
import json
import sys
from pathlib import Path


def option(name):
    index = sys.argv.index(name)
    return Path(sys.argv[index + 1])


if sys.argv[1] == "-c":
    raise SystemExit(0)

invoked = Path(sys.argv[1]).name
if invoked == "benchmark.py":
    output_dir = option("--output-dir")
    if not output_dir.is_dir():
        print("benchmark output directory does not exist", file=sys.stderr)
        raise SystemExit(71)
    if any(output_dir.iterdir()):
        print("benchmark output directory was not empty", file=sys.stderr)
        raise SystemExit(72)
    (output_dir / "empty-output-dir-observed").write_text("yes\n")
    (output_dir / "summary.json").write_text("{}\n")
    print("fake benchmark completed")
elif invoked == "make_public_summary.py":
    option("--json-out").write_text(json.dumps({"status": "ok"}) + "\n")
    option("--csv-out").write_text("status\nok\n")
else:
    print(f"unexpected Python invocation: {invoked}", file=sys.stderr)
    raise SystemExit(73)
""",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            environment = dict(os.environ)
            environment["BENCH_PYTHON"] = str(fake_python)

            completed = subprocess.run(
                [
                    str(runner),
                    "--label",
                    "runner-log-integration",
                    "--artifact-root",
                    str(artifact_root),
                ],
                cwd=HERE.parent,
                env=environment,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            run_dirs = [path for path in artifact_root.iterdir() if path.is_dir()]
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            self.assertEqual(
                (run_dir / "empty-output-dir-observed").read_text(), "yes\n"
            )
            self.assertIn("fake benchmark completed", completed.stdout)
            self.assertIn(
                "fake benchmark completed", (run_dir / "runner.log").read_text()
            )
            self.assertFalse(list(artifact_root.glob("*.runner.log")))

    def test_runner_refuses_to_repermission_existing_or_symlinked_root(self) -> None:
        runner = HERE / "run_mia3_fixed1024.sh"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fake_python = temp / "fake-python"
            fake_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_python.chmod(0o755)
            environment = dict(os.environ)
            environment["BENCH_PYTHON"] = str(fake_python)
            shared = temp / "shared"
            shared.mkdir(mode=0o755)
            linked = temp / "linked"
            linked.symlink_to(shared, target_is_directory=True)

            for artifact_root in (shared, linked):
                completed = subprocess.run(
                    [
                        str(runner),
                        "--label",
                        "permissions-test",
                        "--artifact-root",
                        str(artifact_root),
                    ],
                    cwd=HERE.parent,
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("Artifact root", completed.stderr)

            self.assertEqual(shared.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
