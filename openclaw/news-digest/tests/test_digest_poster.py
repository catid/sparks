"""Offline regression tests for transactional Slack digest delivery."""

import importlib.util
import os
import stat
import sys
import tempfile
import time
import unittest
import uuid

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from news_digest import Digest, Item, StateDB, render_slack_sections


POSTER_PATH = os.path.join(PROJECT_DIR, "digest-poster.py")
POSTER_SPEC = importlib.util.spec_from_file_location(
    "digest_poster", POSTER_PATH,
)
if POSTER_SPEC is None or POSTER_SPEC.loader is None:
    raise RuntimeError("could not load digest-poster.py")
digest_poster = importlib.util.module_from_spec(POSTER_SPEC)
POSTER_SPEC.loader.exec_module(digest_poster)


def _item(index, category="Fixture", long_summary=False):
    summary = ""
    if long_summary:
        summary = ("Detailed offline summary %d. " % index) * 30
    return Item(
        uid="item-%02d" % index,
        source="Source %02d" % index,
        source_key="source-%02d" % index,
        categories=(category,),
        title="Offline item %02d" % index,
        url="https://example.test/items/%02d" % index,
        summary=summary,
        published_at=time.time() - index,
    )


def _digest(items_by_category):
    sections = {
        category: list(items)
        for category, items in items_by_category.items()
    }
    return Digest(
        sections=sections,
        statuses=[],
        discovered_count=sum(len(items) for items in sections.values()),
        eligible_count=sum(len(items) for items in sections.values()),
        selected_count=sum(len(items) for items in sections.values()),
        generated_at=1_785_000_000.0,
    )


class RecordingClient:
    def __init__(self, root_ts="123.456", fail_at=None):
        self.root_ts = root_ts
        self.fail_at = fail_at
        self.calls = []

    def post_message(
        self,
        channel,
        text,
        thread_ts=None,
        client_msg_id=None,
    ):
        self.calls.append({
            "channel": channel,
            "text": text,
            "thread_ts": thread_ts,
            "client_msg_id": client_msg_id,
        })
        if self.fail_at is not None and len(self.calls) == self.fail_at:
            raise digest_poster.DeliveryError("synthetic Slack failure")
        return self.root_ts


class SplitAndLockTests(unittest.TestCase):
    def test_split_respects_limit_without_losing_words(self):
        text = (
            "alpha beta gamma delta\n\n"
            "epsilon zeta eta theta\n"
            "iota kappa lambda"
        )

        chunks = digest_poster.split_slack_text(text, limit=24)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk and len(chunk) <= 24 for chunk in chunks))
        self.assertEqual(text.split(), " ".join(chunks).split())

    def test_lock_rejects_overlap_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = os.path.join(temp_dir, "digest.lock")
            with digest_poster.exclusive_lock(lock_path):
                self.assertEqual(
                    0o600,
                    stat.S_IMODE(os.stat(lock_path).st_mode),
                )
                with self.assertRaises(digest_poster.AlreadyRunningError):
                    with digest_poster.exclusive_lock(lock_path):
                        self.fail("overlapping lock unexpectedly succeeded")

            with digest_poster.exclusive_lock(lock_path):
                pass


class TransactionalDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.temp_dir.name, "state.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _discover(self, items, now=None):
        with StateDB(self.state_path) as state:
            state.upsert_discovered(items, now=now)

    def _emitted(self, items):
        with StateDB(self.state_path) as state:
            return {
                item.uid: state.is_emitted(item.uid)
                for item in items
            }

    def test_successful_sections_are_committed(self):
        first = _item(1, category="First")
        second = _item(2, category="Second")
        unselected = _item(3, category="Not selected")
        items = [first, second]
        digest = _digest({"First": [first], "Second": [second]})
        self._discover(items + [unselected], now=digest.generated_at)
        client = RecordingClient()

        root_ts, posted_sections = digest_poster.deliver_digest(
            digest, self.state_path, "C_TEST", client,
        )

        self.assertEqual("123.456", root_ts)
        self.assertEqual(2, posted_sections)
        self.assertEqual({first.uid: True, second.uid: True},
                         self._emitted(items))
        self.assertIsNone(client.calls[0]["thread_ts"])
        self.assertTrue(all(
            call["thread_ts"] == root_ts for call in client.calls[1:]
        ))
        with StateDB(self.state_path) as state:
            self.assertEqual(digest.generated_at, state.last_completed_at())
            self.assertEqual([], state.pending_items(0))
            self.assertFalse(state.is_emitted(unselected.uid))

    def test_failed_multichunk_section_is_not_committed(self):
        items = [_item(index, long_summary=True) for index in range(10)]
        digest = _digest({"Long section": items})
        self._discover(items)
        section_text = render_slack_sections(digest)[0][1]
        chunks = digest_poster.split_slack_text(section_text)
        self.assertGreater(len(chunks), 1)
        client = RecordingClient(fail_at=3)

        with self.assertRaises(digest_poster.DeliveryError):
            digest_poster.deliver_digest(
                digest, self.state_path, "C_TEST", client,
            )

        self.assertEqual(3, len(client.calls))
        self.assertTrue(all(
            emitted is False for emitted in self._emitted(items).values()
        ))
        with StateDB(self.state_path) as state:
            self.assertEqual(0, state.last_completed_at())
            self.assertEqual(len(items), len(state.pending_items(0)))

    def test_empty_success_advances_watermark_without_posting(self):
        digest = _digest({})
        client = RecordingClient()

        root_ts, posted_sections = digest_poster.deliver_digest(
            digest, self.state_path, "C_TEST", client,
        )

        self.assertEqual("", root_ts)
        self.assertEqual(0, posted_sections)
        self.assertEqual([], client.calls)
        with StateDB(self.state_path) as state:
            self.assertEqual(digest.generated_at, state.last_completed_at())

    def test_manifest_reuses_exact_chunks_and_ids_across_retry(self):
        items = [_item(index, long_summary=True) for index in range(10)]
        digest = _digest({"Stable section": items})
        self._discover(items)
        first_client = RecordingClient(root_ts="111.1", fail_at=3)
        second_client = RecordingClient(root_ts="222.2")

        with self.assertRaises(digest_poster.DeliveryError):
            digest_poster.deliver_digest(
                digest, self.state_path, "C_TEST", first_client,
            )
        with StateDB(self.state_path) as state:
            active = state.load_active_delivery("C_TEST")
        self.assertIsNotNone(active)
        digest_poster.deliver_manifest(
            active, self.state_path, "C_TEST", second_client,
        )

        first_section_calls = first_client.calls[1:]
        self.assertGreaterEqual(
            len(second_client.calls), len(first_section_calls)
        )
        self.assertEqual(
            first_section_calls,
            second_client.calls[:len(first_section_calls)],
        )
        self.assertTrue(all(
            call["thread_ts"] == "111.1" for call in second_client.calls
        ))
        all_ids = [
            call["client_msg_id"]
            for call in first_client.calls[:1] + second_client.calls
        ]
        self.assertTrue(all(value for value in all_ids))
        self.assertTrue(all(str(uuid.UUID(value)) == value for value in all_ids))
        self.assertTrue(all(self._emitted(items).values()))
        with StateDB(self.state_path) as state:
            self.assertIsNone(state.load_active_delivery("C_TEST"))

    def test_manifest_ids_are_channel_scoped(self):
        items = [_item(1)]
        digest = _digest({"Fixture": items})
        sections = render_slack_sections(digest)

        first_key, first = digest_poster.compile_delivery_manifest(
            digest, "C_ONE", "Overview", sections, "deterministic",
        )
        second_key, second = digest_poster.compile_delivery_manifest(
            digest, "C_TWO", "Overview", sections, "deterministic",
        )

        self.assertNotEqual(first_key, second_key)
        self.assertNotEqual(
            first["root"]["client_msg_id"],
            second["root"]["client_msg_id"],
        )


if __name__ == "__main__":
    unittest.main()
