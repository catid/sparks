"""Static safety tests; these do not contact Docker or a model endpoint."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from run_agent_eval import (
    DockerSandbox,
    GRADE_SUCCESS_MARKER,
    HarnessError,
    REQUEST_CONSTANTS,
    build_render_request,
    build_request,
    changed_protected_files,
    exact_turn_token_budget,
    final_stop_reason,
    grade_marker_seen,
    protected_hashes,
    resolve_token_budget,
    validate_shell_command,
    validate_tasks,
)
from tasks import TASKS


class ShellPolicyTests(unittest.TestCase):
    def assert_allowed(self, command: str) -> None:
        result = validate_shell_command(command)
        self.assertTrue(result.allowed, result.reason)

    def assert_denied(self, command: str) -> None:
        result = validate_shell_command(command)
        self.assertFalse(result.allowed)
        self.assertTrue(result.reason)

    def test_normal_repository_work_is_allowed(self):
        self.assert_allowed("ls -la && sed -n '1,200p' ledger.py")
        self.assert_allowed("python -m unittest discover -s tests -v")
        self.assert_allowed(
            """python - <<'PY'\nfrom pathlib import Path\n"""
            """Path("x.txt").write_text("ok")\nPY""",
        )
        self.assert_allowed(
            """python - <<'PY'\nfrom pathlib import Path\n"""
            """print(Path("base") / "child.txt")\nPY""",
        )
        self.assert_allowed("cd /workspace && python tests/test_ledger.py")
        self.assert_allowed("printf '%s\\n' ok > /tmp/result")
        self.assert_allowed(
            "python -m unittest -v 2>&1 || true",
        )
        self.assert_allowed("python check.py >&2")
        self.assert_allowed("python check.py &> /tmp/check.log")

    def test_host_and_container_escape_primitives_are_denied(self):
        self.assert_denied("cat /home/operator/.ssh/id_ed25519")
        self.assert_denied("cat /var/run/docker.sock")
        self.assert_denied("docker ps")
        self.assert_denied("nsenter --target 1 --mount")
        self.assert_denied("mount /dev/sda /tmp/x")
        self.assert_denied("cat ../outside")
        self.assert_denied("find / -maxdepth 2")

    def test_network_service_and_background_commands_are_denied(self):
        self.assert_denied("curl https://example.com")
        self.assert_denied("python server.py &")
        self.assert_denied("python server.py 2>&1 &")
        self.assert_denied("sudo systemctl restart openclaw")
        self.assert_denied("reboot")


class RequestTests(unittest.TestCase):
    def test_max_thinking_request_is_exact(self):
        payload = build_request(
            "deepseek-v4-flash-nvfp4",
            [{"role": "user", "content": "test"}],
            131_072,
        )
        self.assertEqual(payload["temperature"], 1.0)
        self.assertEqual(payload["top_p"], 1.0)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(payload["max_tokens"], 131_072)
        self.assertTrue(payload["chat_template_kwargs"]["thinking"])
        self.assertTrue(
            payload["chat_template_kwargs"]["enable_thinking"],
        )
        self.assertEqual(
            payload["chat_template_kwargs"]["reasoning_effort"],
            "max",
        )
        self.assertEqual(
            payload["reasoning_effort"],
            REQUEST_CONSTANTS["reasoning_effort"],
        )

    def test_auto_fit_token_budget_uses_exact_remaining_context(self):
        context, max_tokens = resolve_token_budget(
            33_554,
            None,
            execute=True,
        )
        self.assertEqual(context, 33_554)
        self.assertIsNone(max_tokens)
        self.assertEqual(
            exact_turn_token_budget(context, 1_234, max_tokens),
            32_320,
        )
        self.assertEqual(
            exact_turn_token_budget(context, 1_234, 8_192),
            8_192,
        )
        self.assertEqual(
            exact_turn_token_budget(context, 30_000, 8_192),
            3_554,
        )
        with self.assertRaisesRegex(HarnessError, "exhausting"):
            exact_turn_token_budget(context, context, None)
        with self.assertRaisesRegex(HarnessError, "required with --execute"):
            resolve_token_budget(None, None, execute=True)
        with self.assertRaisesRegex(HarnessError, "exceeds"):
            resolve_token_budget(33_554, 40_000, execute=True)

    def test_render_preflight_matches_completion_rendering_inputs(self):
        messages = [{"role": "user", "content": "test"}]
        payload = build_render_request(
            "deepseek-v4-flash-nvfp4",
            messages,
        )
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["tools"][0]["function"]["name"], "exec")
        self.assertEqual(payload["max_tokens"], 1)
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(
            payload["chat_template_kwargs"],
            REQUEST_CONSTANTS["chat_template_kwargs"],
        )

    def test_truncated_or_empty_response_is_not_final(self):
        self.assertEqual(
            final_stop_reason("partial", "length"),
            "output_truncated",
        )
        self.assertEqual(
            final_stop_reason("", "stop"),
            "empty_assistant_final",
        )
        self.assertEqual(
            final_stop_reason("done", "stop"),
            "assistant_final",
        )

    def test_hidden_grade_requires_exact_marker_line(self):
        self.assertTrue(grade_marker_seen({"output": "x\nHIDDEN_OK\n"}))
        self.assertFalse(grade_marker_seen({"output": "HIDDEN_OK-ish"}))
        self.assertFalse(grade_marker_seen({"output": ""}))

    def test_tasks_are_statically_safe(self):
        validate_tasks()
        self.assertEqual(
            {task.name for task in TASKS},
            {"ledger_bugfix", "retry_queue_debug", "worker_pool_cancel"},
        )
        for task in TASKS:
            self.assertTrue(task.grade_command.startswith("python -I -B -"))
            self.assertIn(GRADE_SUCCESS_MARKER, task.grade_command)

    def test_test_mutation_is_detected_by_host_hash(self):
        task = TASKS[0]
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            for relative, content in task.files.items():
                destination = workspace / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            expected = protected_hashes(workspace, task)
            test_path = workspace / "tests/test_ledger.py"
            test_path.write_text("pass\n", encoding="utf-8")
            self.assertEqual(
                changed_protected_files(workspace, expected),
                ["tests/test_ledger.py"],
            )
            test_path.write_text(
                task.files["tests/test_ledger.py"],
                encoding="utf-8",
            )
            (workspace / "tests/test_bypass.py").write_text(
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            self.assertEqual(
                changed_protected_files(workspace, expected),
                ["tests/test_bypass.py"],
            )


class DockerBoundaryTests(unittest.TestCase):
    def test_model_command_is_only_container_bash_argv(self):
        class EmptyOutput:
            def read(self, _size: int) -> bytes:
                return b""

        class CompletedProcess:
            stdout = EmptyOutput()

            @staticmethod
            def poll() -> int:
                return 0

            @staticmethod
            def wait(timeout: int) -> int:
                del timeout
                return 0

            @staticmethod
            def kill() -> None:
                return None

        sandbox = DockerSandbox(
            docker=("docker",),
            workspace=Path("/tmp/offline-workspace"),
            image="python:3.12-slim",
            name="offline-test",
            active=True,
        )
        command = "printf '%s\\n' container-only"
        with patch(
            "run_agent_eval.subprocess.Popen",
            return_value=CompletedProcess(),
        ) as popen:
            result = sandbox.execute(command, 5)
        self.assertEqual(result["exit_code"], 0)
        invocation = popen.call_args.args[0]
        self.assertIsInstance(invocation, list)
        self.assertEqual(invocation[-5:-1], [
            "bash",
            "--noprofile",
            "--norc",
            "-lc",
        ])
        self.assertEqual(invocation[-1], command)
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_image_cannot_be_reparsed_as_docker_option(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        sandbox = DockerSandbox(
            docker=("docker",),
            workspace=Path("/tmp/offline-workspace"),
            image="--privileged",
            name="offline-test",
        )
        with patch(
            "run_agent_eval.subprocess.run",
            return_value=completed,
        ) as run:
            sandbox.start()
        invocation = run.call_args.args[0]
        image_index = invocation.index("--privileged")
        self.assertEqual(invocation[image_index - 1], "--")
        mount = invocation[invocation.index("--mount") + 1]
        self.assertNotIn(",rw", mount)
        self.assertNotIn("shell", run.call_args.kwargs)
        sandbox.active = False


if __name__ == "__main__":
    unittest.main()
