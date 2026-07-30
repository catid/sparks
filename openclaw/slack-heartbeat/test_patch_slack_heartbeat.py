#!/usr/bin/env python3
"""Unit tests for the guarded Slack heartbeat source transform."""

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("patch-slack-heartbeat.py")
SPEC = importlib.util.spec_from_file_location("slack_heartbeat_patcher", MODULE_PATH)
assert SPEC and SPEC.loader
PATCHER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PATCHER
SPEC.loader.exec_module(PATCHER)


def source_fixture():
    return "\n".join(
        (
            "before",
            PATCHER.CONSTANTS_ANCHOR,
            "middle-a",
            PATCHER.DECLARATIONS_ANCHOR,
            "middle-b",
            PATCHER.TRY_ANCHOR,
            "middle-c",
            PATCHER.FINALLY_ANCHOR,
            "after",
        )
    )


class PatchTextTests(unittest.TestCase):
    def test_adds_exact_heartbeat_lifecycle(self):
        patched = PATCHER.patch_text(source_fixture())

        self.assertIn(f"// {PATCHER.PATCH_MARKER}", patched)
        self.assertIn(
            "SLACK_THINKING_HEARTBEAT_INTERVAL_MS = 5e3",
            patched,
        )
        self.assertIn(
            '".".repeat(Math.min(thinkingHeartbeatDots, '
            "SLACK_THINKING_HEARTBEAT_MAX_DOTS))",
            patched,
        )
        self.assertIn("await draftStream.flush();", patched)
        self.assertIn("stopSlackThinkingHeartbeat();", patched)
        self.assertNotIn("draftPreviewCommitted", patched)

    def test_transform_is_idempotent(self):
        once = PATCHER.patch_text(source_fixture())
        twice = PATCHER.patch_text(once)
        self.assertEqual(once, twice)

    def test_rejects_missing_anchor(self):
        source = source_fixture().replace(PATCHER.TRY_ANCHOR, "")
        with self.assertRaisesRegex(PATCHER.PatchError, "anchor count is 0"):
            PATCHER.patch_text(source)

    def test_rejects_duplicate_anchor(self):
        source = source_fixture() + "\n" + PATCHER.FINALLY_ANCHOR
        with self.assertRaisesRegex(PATCHER.PatchError, "anchor count is 2"):
            PATCHER.patch_text(source)

    def test_rejects_partial_patch(self):
        source = source_fixture() + "\nconst startSlackThinkingHeartbeat = 1;"
        with self.assertRaisesRegex(PATCHER.PatchError, "partial heartbeat"):
            PATCHER.patch_text(source)

    def test_rejects_corrupt_marked_patch(self):
        source = source_fixture() + f"\n// {PATCHER.PATCH_MARKER}"
        with self.assertRaisesRegex(PATCHER.PatchError, "patched five-second"):
            PATCHER.patch_text(source)

    def test_rejects_unsafe_error_cleanup_revision(self):
        patched = PATCHER.patch_text(source_fixture())
        unsafe = (
            patched
            + "\nif (!draftPreviewCommitted) await draftStream?.clear();"
        )
        with self.assertRaisesRegex(PATCHER.PatchError, "already finalized"):
            PATCHER.patch_text(unsafe)

    def test_rejects_target_changed_after_preflight(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = pathlib.Path(temporary) / "pipeline.runtime-fixture.js"
            target.write_bytes(b"original")
            plan = PATCHER.PatchPlan(
                plugin_dir=pathlib.Path(temporary),
                target=target,
                original=b"original",
                patched=b"patched",
                already_patched=False,
                mode=0o644,
            )
            PATCHER.require_target_unchanged(plan)
            target.write_bytes(b"concurrent update")
            with self.assertRaisesRegex(
                PATCHER.PatchError,
                "changed after preflight",
            ):
                PATCHER.require_target_unchanged(plan)

    def test_multi_target_failure_rolls_back_earlier_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            first = root / "first.js"
            second = root / "second.js"
            first.write_bytes(b"original-first")
            second.write_bytes(b"original-second")
            plans = (
                PATCHER.PatchPlan(
                    plugin_dir=root / "plugin-first",
                    target=first,
                    original=b"original-first",
                    patched=b"patched-first",
                    already_patched=False,
                    mode=0o644,
                ),
                PATCHER.PatchPlan(
                    plugin_dir=root / "plugin-second",
                    target=second,
                    original=b"original-second",
                    patched=b"patched-second",
                    already_patched=False,
                    mode=0o644,
                ),
            )
            real_atomic_replace = PATCHER.atomic_replace

            def fail_second_patch(path, data, mode):
                if path == second and data == b"patched-second":
                    raise OSError("simulated second-target failure")
                real_atomic_replace(path, data, mode)

            with mock.patch.object(PATCHER, "check_candidate_syntax"):
                with mock.patch.object(PATCHER, "run_node_check"):
                    with mock.patch.object(
                        PATCHER,
                        "atomic_replace",
                        side_effect=fail_second_patch,
                    ):
                        with self.assertRaisesRegex(
                            OSError,
                            "simulated second-target failure",
                        ):
                            PATCHER.apply_plans(
                                plans,
                                pathlib.Path("/unused/node"),
                                str(root / "backups"),
                            )

            self.assertEqual(first.read_bytes(), b"original-first")
            self.assertEqual(second.read_bytes(), b"original-second")


if __name__ == "__main__":
    unittest.main()
