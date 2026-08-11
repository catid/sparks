#!/usr/bin/env python3

import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest


HELPER_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "scripts"
    / "terminate-kiosk-session.py"
)
SPEC = importlib.util.spec_from_file_location("c3_kiosk_session_cleanup", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)


class SessionFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.path = self.root / "session-id"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, payload: bytes, mode: int = 0o600) -> None:
        self.path.write_bytes(payload)
        self.path.chmod(mode)

    def test_reads_private_numeric_id(self) -> None:
        self.write(b"66\n")
        self.assertEqual(cleanup.read_session_id(self.path, os.getuid()), "66")

    def test_missing_file_is_idempotent(self) -> None:
        self.assertIsNone(cleanup.read_session_id(self.path, os.getuid()))

    def test_rejects_symlink_broad_mode_and_non_numeric_payload(self) -> None:
        target = self.root / "target"
        target.write_text("66\n", encoding="ascii")
        self.path.symlink_to(target)
        with self.assertRaises(cleanup.CleanupError):
            cleanup.read_session_id(self.path, os.getuid())
        self.path.unlink()

        self.write(b"66\n", 0o640)
        with self.assertRaises(cleanup.CleanupError):
            cleanup.read_session_id(self.path, os.getuid())

        self.write(b"session-66\n")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.read_session_id(self.path, os.getuid())

    def test_rejects_wrong_owner_contract(self) -> None:
        self.write(b"66\n")
        with self.assertRaises(cleanup.CleanupError):
            cleanup.read_session_id(self.path, os.getuid() + 1)

    def test_first_upgrade_can_resolve_the_tracked_main_pid(self) -> None:
        process = self.root / "31588"
        process.mkdir()
        (process / "status").write_text(
            f"Name:\tstartx\nUid:\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\t{os.getuid()}\n",
            encoding="ascii",
        )
        (process / "sessionid").write_text("66\n", encoding="ascii")
        self.assertEqual(
            cleanup.session_id_from_main_pid(31588, os.getuid(), self.root), "66"
        )
        with self.assertRaises(cleanup.CleanupError):
            cleanup.session_id_from_main_pid(31588, os.getuid() + 1, self.root)


class LoginSessionTests(unittest.TestCase):
    identity = {
        "Name": "catid",
        "User": "1000",
        "Service": "login",
        "TTY": "tty7",
        "Remote": "no",
    }

    def runner(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        property_name = command[3].split("=", 1)[1]
        return subprocess.CompletedProcess(command, 0, self.identity[property_name] + "\n", "")

    def test_accepts_exact_local_vt7_login(self) -> None:
        self.assertTrue(
            cleanup.validate_session("66", "catid", 1000, "tty7", self.runner)
        )

    def test_first_upgrade_also_requires_the_tracked_session_leader(self) -> None:
        identity = dict(self.identity)
        identity["Leader"] = "31588"

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            name = command[3].split("=", 1)[1]
            return subprocess.CompletedProcess(command, 0, identity[name] + "\n", "")

        self.assertTrue(
            cleanup.validate_session(
                "66", "catid", 1000, "tty7", runner, expected_leader=31588
            )
        )
        with self.assertRaises(cleanup.CleanupError):
            cleanup.validate_session(
                "66", "catid", 1000, "tty7", runner, expected_leader=99999
            )

    def test_rejects_an_ssh_or_other_users_session(self) -> None:
        for key, value in (("Remote", "yes"), ("TTY", ""), ("Name", "someone")):
            with self.subTest(key=key):
                changed = dict(self.identity)
                changed[key] = value

                def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                    name = command[3].split("=", 1)[1]
                    return subprocess.CompletedProcess(command, 0, changed[name] + "\n", "")

                with self.assertRaises(cleanup.CleanupError):
                    cleanup.validate_session("66", "catid", 1000, "tty7", runner)

    def test_disappeared_session_is_idempotent(self) -> None:
        def missing(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "No session '66' known")

        self.assertFalse(
            cleanup.validate_session("66", "catid", 1000, "tty7", missing)
        )

    def test_logind_query_failure_is_not_mistaken_for_a_closed_session(self) -> None:
        def failed(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 1, "", "Failed to connect to bus")

        with self.assertRaises(cleanup.CleanupError):
            cleanup.validate_session("66", "catid", 1000, "tty7", failed)

    def test_termination_uses_literal_numeric_id(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        cleanup.terminate_session("66", runner)
        self.assertEqual(calls, [[cleanup.SYSTEMCTL, "stop", "session-66.scope"]])

    def test_scope_disappearing_during_stop_is_idempotent(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if command[0] == cleanup.SYSTEMCTL:
                return subprocess.CompletedProcess(command, 5, "", "Unit not loaded")
            return subprocess.CompletedProcess(command, 1, "", "No session '66' known")

        cleanup.terminate_session("66", runner)
        self.assertEqual(calls[0], [cleanup.SYSTEMCTL, "stop", "session-66.scope"])


if __name__ == "__main__":
    unittest.main()
