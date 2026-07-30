"""Regression tests for the news digest collector.

The suite intentionally uses only synthetic data and temporary SQLite files.
It documents the small public API used by the tests:

* ``parse_feed(xml, FeedSpec(...))``
* ``canonicalize_url(url)`` and ``item_id(...)``
* ``render_item_markdown(item)``
* ``match_interest_tags(title, summary)``
* ``select_diverse(items, emitted_uids, ...)``
* ``StateDB(path)``
* ``parse_hf_trending(payload)`` and ``parse_hf_daily_papers(payload)``

``expand_x_urls`` is tested when it is intentionally exposed by the module.
"""

import importlib
import dataclasses
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(HERE)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

news_digest = importlib.import_module("news_digest")


RSS2 = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>Fixture RSS</title>
    <item>
      <guid isPermaLink="false">rss-guid-1</guid>
      <title>
        A   spaced title
      </title>
      <link>
        https://example.test/posts/one?utm_source=rss&amp;x=1
      </link>
      <description><![CDATA[
        This is a deliberately long teaser which must not replace the
        content:encoded body merely because it contains more characters.
      ]]></description>
      <content:encoded><![CDATA[
        <p>The complete article summary.</p>
      ]]></content:encoded>
      <dc:creator> Ada Example </dc:creator>
      <pubDate> Wed, 29 Jul 2026 18:45:00 GMT </pubDate>
    </item>
  </channel>
</rss>
"""


ATOM = """\
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Fixture Atom</title>
  <entry>
    <id>tag:example.test,2026:atom-one</id>
    <title type="html">Atom &amp; entry</title>
    <updated>2026-07-29T19:15:00Z</updated>
    <link rel="self" href="https://example.test/api/atom-one"/>
    <link rel="alternate"
          href="https://example.test/posts/atom-one?ref=feed"/>
    <author><name>Grace Example</name></author>
    <content type="html">&lt;p&gt;Content wins when summary is absent.&lt;/p&gt;</content>
  </entry>
</feed>
"""


RSS1_RDF = """\
<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF
    xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
    xmlns="http://purl.org/rss/1.0/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel rdf:about="https://nature.example.test/">
    <title>Fixture RDF</title>
    <link>https://nature.example.test/</link>
  </channel>
  <item rdf:about="https://nature.example.test/articles/rdf-one">
    <title>RDF article</title>
    <link>
      https://nature.example.test/articles/rdf-one
    </link>
    <dc:date>2026-07-29T20:30:00Z</dc:date>
    <dc:creator>Nature Author</dc:creator>
    <description>Fallback RDF description.</description>
    <content:encoded><![CDATA[<p>Full RDF content.</p>]]></content:encoded>
  </item>
</rdf:RDF>
"""


def _value(item, name, *aliases):
    """Read a normalized field from either an Item or a mapping."""
    for candidate in (name,) + aliases:
        if hasattr(item, candidate):
            return getattr(item, candidate)
        try:
            return item[candidate]
        except (KeyError, TypeError):
            pass
    raise AssertionError("normalized item has no %r field" % name)


def _feed_spec(category, source, url):
    """Build a FeedSpec while keeping fixture setup independent of field order."""
    values = {
        "key": source.lower().replace(" ", "-"),
        "category": category,
        "categories": (category,),
        "source": source,
        "name": source,
        "label": source,
        "url": url,
        "kind": "rss",
        "feed_type": "rss",
    }
    kwargs = {}
    for field in dataclasses.fields(news_digest.FeedSpec):
        if field.name in values:
            kwargs[field.name] = values[field.name]
        elif (
            field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ):
            raise AssertionError(
                "test fixture needs a value for FeedSpec.%s" % field.name
            )
    return news_digest.FeedSpec(**kwargs)


def _item(title, url, source, published, summary=""):
    """Build an Item fixture from the module's normalized dataclass."""
    published_at = float(published)
    uid = news_digest.item_id(source, "", url, title, published_at)
    values = {
        "uid": uid,
        "id": uid,
        "item_id": uid,
        "category": "AI / LLMs",
        "categories": ("AI / LLMs",),
        "source": source,
        "source_key": source,
        "source_name": source,
        "title": title,
        "url": url,
        "canonical_url": news_digest.canonicalize_url(url),
        "summary": summary,
        "abstract": "",
        "published": published_at,
        "published_at": published_at,
        "raw_id": "",
        "feed_id": "",
        "authors": [],
        "institutes": [],
        "affiliation": "",
        "engagement": 0,
        "tags": (),
        "kind": "rss",
    }
    kwargs = {}
    for field in dataclasses.fields(news_digest.Item):
        if field.name in values:
            kwargs[field.name] = values[field.name]
        elif (
            field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ):
            raise AssertionError(
                "test fixture needs a value for Item.%s" % field.name
            )
    return news_digest.Item(**kwargs)


def _close_store(store):
    close = getattr(store, "close", None)
    if close is not None:
        close()
    else:
        store.__exit__(None, None, None)


class FeedParsingTests(unittest.TestCase):
    def test_rss2_content_date_and_whitespace(self):
        items = news_digest.parse_feed(
            RSS2,
            _feed_spec(
                "Fixture", "Fixture RSS", "https://example.test/feed.xml"
            ),
        )

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("A spaced title", _value(item, "title"))
        self.assertEqual(_value(item, "url").strip(), _value(item, "url"))
        self.assertIn("https://example.test/posts/one", _value(item, "url"))
        self.assertIn("x=1", _value(item, "url"))
        self.assertIn(
            "complete article summary", _value(item, "summary").lower()
        )
        self.assertNotIn("<p>", _value(item, "summary"))
        self.assertEqual(["Ada Example"], list(_value(item, "authors")))
        self.assertEqual(
            datetime(2026, 7, 29, 18, 45, tzinfo=timezone.utc).timestamp(),
            _value(item, "published_at", "published"),
        )
        self.assertEqual("rss-guid-1", _value(item, "raw_id", "feed_id"))

    def test_atom_alternate_link_content_and_iso_date(self):
        items = news_digest.parse_feed(
            ATOM,
            _feed_spec(
                "Fixture", "Fixture Atom", "https://example.test/atom.xml"
            ),
        )

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("Atom & entry", _value(item, "title"))
        self.assertEqual(_value(item, "url").strip(), _value(item, "url"))
        self.assertIn(
            "https://example.test/posts/atom-one", _value(item, "url")
        )
        self.assertEqual(
            "Content wins when summary is absent.", _value(item, "summary")
        )
        self.assertEqual(["Grace Example"], list(_value(item, "authors")))
        self.assertEqual(
            datetime(2026, 7, 29, 19, 15, tzinfo=timezone.utc).timestamp(),
            _value(item, "published_at", "published"),
        )
        self.assertEqual(
            "tag:example.test,2026:atom-one",
            _value(item, "raw_id", "feed_id"),
        )

    def test_rss1_rdf_items_are_not_lost(self):
        items = news_digest.parse_feed(
            RSS1_RDF,
            _feed_spec(
                "Fixture",
                "Fixture RDF",
                "https://nature.example.test/nature.rss",
            ),
        )

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("RDF article", _value(item, "title"))
        self.assertEqual(
            "https://nature.example.test/articles/rdf-one",
            _value(item, "url"),
        )
        self.assertEqual("Full RDF content.", _value(item, "summary"))
        self.assertEqual(["Nature Author"], list(_value(item, "authors")))
        self.assertEqual(
            datetime(2026, 7, 29, 20, 30, tzinfo=timezone.utc).timestamp(),
            _value(item, "published_at", "published"),
        )


class IdentityTests(unittest.TestCase):
    def test_canonicalize_url_removes_tracking_fragment_and_whitespace(self):
        canonical = news_digest.canonicalize_url(
            "  HTTPS://Example.TEST:443/a/../paper/?b=2"
            "&utm_source=x&utm_medium=social#discussion  "
        )
        self.assertEqual("https://example.test/paper?b=2", canonical)

    def test_canonical_id_is_stable_for_equivalent_urls(self):
        self.assertEqual(
            news_digest.item_id(
                "fixture",
                "",
                "https://example.test/paper?id=7&utm_campaign=digest",
                "Paper",
                None,
            ),
            news_digest.item_id(
                "fixture",
                "",
                "https://EXAMPLE.test:443/paper?utm_source=rss&id=7#top",
                "Paper",
                None,
            ),
        )

    def test_feed_id_is_scoped_to_source(self):
        self.assertNotEqual(
            news_digest.item_id(
                "https://one.example/feed", "post-42", "", "", None
            ),
            news_digest.item_id(
                "https://two.example/feed", "post-42", "", "", None
            ),
        )

    def test_feed_id_wins_over_mutable_title_and_url(self):
        self.assertEqual(
            news_digest.item_id(
                "fixture",
                "post-42",
                "https://example.test/old",
                "Old",
                None,
            ),
            news_digest.item_id(
                "fixture",
                "post-42",
                "https://example.test/new",
                "Edited title",
                None,
            ),
        )


class RenderingAndTagTests(unittest.TestCase):
    def test_renderer_escapes_markdown_and_uses_summary_if_abstract_empty(self):
        item = _item(
            "A [model] (fast) *release*",
            "https://example.test/a_(release)",
            "fixture",
            10,
            "Fallback <summary> & useful details.",
        )

        rendered = news_digest.render_item_markdown(item)

        self.assertIn(r"A \[model\] (fast) \*release\*", rendered)
        self.assertIn("Fallback & useful details.", rendered)
        self.assertNotIn("<summary>", rendered)
        self.assertNotIn("]()", rendered)

    def test_renderer_includes_affiliations(self):
        item = _item(
            "Affiliated work",
            "https://example.test/work",
            "fixture",
            10,
            "Details.",
        )
        item = dataclasses.replace(
            item,
            authors=["A. Researcher"],
            institutes=["Example AI Lab"],
        )
        self.assertIn(
            "Example AI Lab", news_digest.render_item_markdown(item)
        )

    def test_short_keywords_use_word_boundaries(self):
        false_positive_tags = news_digest.match_interest_tags(
            "Champion of the world",
            "A colorful exploration of games and geography.",
        )
        joined = " ".join(false_positive_tags).lower()
        self.assertNotIn("rl / agents", joined)
        self.assertNotIn("optimizers/training", joined)

        true_positive_tags = news_digest.match_interest_tags(
            "Pion and RL improve training",
            "We study RL agents with the Pion optimizer.",
        )
        joined = " ".join(true_positive_tags).lower()
        self.assertIn("rl / agents", joined)
        self.assertIn("optimizers/training", joined)


class SelectionTests(unittest.TestCase):
    def test_selection_is_recent_and_source_diverse(self):
        items = [
            _item("A1", "https://a/1", "source-a", 100),
            _item("A2", "https://a/2", "source-a", 99),
            _item("A3", "https://a/3", "source-a", 98),
            _item("B1", "https://b/1", "source-b", 97),
            _item("C1", "https://c/1", "source-c", 96),
            _item("B2", "https://b/2", "source-b", 95),
        ]

        sections = news_digest.select_diverse(
            items,
            emitted_uids=set(),
            max_per_category=5,
            max_per_source=2,
            x_max_per_source=1,
        )
        selected = sections["AI / LLMs"]

        self.assertEqual(["A1", "B1", "C1", "A2", "B2"],
                         [_value(item, "title") for item in selected])
        sources = [_value(item, "source") for item in selected]
        self.assertLessEqual(sources.count("source-a"), 2)
        self.assertGreaterEqual(len(set(sources)), 3)

    def test_selection_does_not_mutate_input(self):
        items = [
            _item("Older", "https://a/old", "a", 1),
            _item("Newer", "https://b/new", "b", 2),
        ]
        snapshot = list(items)
        news_digest.select_diverse(
            items,
            emitted_uids=set(),
            max_per_category=1,
            max_per_source=1,
            x_max_per_source=1,
        )
        self.assertEqual(snapshot, items)

    def test_selection_excludes_emitted_items(self):
        emitted = _item("Emitted", "https://a/emitted", "a", 2)
        pending = _item("Pending", "https://b/pending", "b", 1)

        sections = news_digest.select_diverse(
            [emitted, pending],
            emitted_uids={_value(emitted, "uid", "id")},
            max_per_category=8,
            max_per_source=2,
            x_max_per_source=1,
        )

        self.assertEqual(
            ["Pending"],
            [_value(item, "title") for item in sections["AI / LLMs"]],
        )

    def test_per_source_limit_is_a_hard_cap(self):
        items = [
            _item("A1", "https://a/1", "source-a", 4),
            _item("A2", "https://a/2", "source-a", 3),
            _item("A3", "https://a/3", "source-a", 2),
            _item("B1", "https://b/1", "source-b", 1),
        ]

        sections = news_digest.select_diverse(
            items,
            emitted_uids=set(),
            max_per_category=8,
            max_per_source=2,
            x_max_per_source=1,
        )

        sources = [
            _value(item, "source") for item in sections["AI / LLMs"]
        ]
        self.assertLessEqual(sources.count("source-a"), 2)


class StateDBTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "digest.sqlite3")
        self.store = news_digest.StateDB(self.db_path)

    def tearDown(self):
        _close_store(self.store)
        self.temp_dir.cleanup()

    def test_discovery_does_not_mark_an_item_emitted(self):
        item = _item("Pending", "https://example.test/pending", "fixture", 10)
        item_uid = _value(item, "uid", "id")

        self.store.upsert_discovered([item])

        self.assertNotIn(item_uid, self.store.emitted_uids())

    def test_only_explicit_delivery_acknowledgement_marks_emitted(self):
        shown = _item("Shown", "https://example.test/shown", "fixture", 10)
        hidden = _item("Not shown", "https://example.test/hidden", "fixture", 9)
        shown_uid = _value(shown, "uid", "id")
        hidden_uid = _value(hidden, "uid", "id")
        self.store.upsert_discovered([shown, hidden])

        self.store.mark_emitted([shown_uid])

        emitted = self.store.emitted_uids()
        self.assertIn(shown_uid, emitted)
        self.assertNotIn(hidden_uid, emitted)

    def test_state_survives_reopen(self):
        item = _item("Durable", "https://example.test/durable", "fixture", 10)
        item_uid = _value(item, "uid", "id")
        self.store.upsert_discovered([item])
        self.store.mark_emitted([item_uid])
        _close_store(self.store)

        self.store = news_digest.StateDB(self.db_path)
        self.assertIn(item_uid, self.store.emitted_uids())

    def test_rediscovery_does_not_clear_emitted_state(self):
        item = _item("Repeat", "https://example.test/repeat", "fixture", 10)
        item_uid = _value(item, "uid", "id")
        self.store.upsert_discovered([item])
        self.store.mark_emitted([item_uid])

        self.store.upsert_discovered([item])

        self.assertIn(item_uid, self.store.emitted_uids())

    def test_pending_payload_survives_source_disappearance(self):
        item = _item(
            "Queued",
            "https://example.test/queued",
            "fixture",
            100,
            summary="Persist the complete normalized item.",
        )
        self.store.upsert_discovered([item])
        _close_store(self.store)

        self.store = news_digest.StateDB(self.db_path)
        pending = self.store.pending_items(0)

        self.assertEqual(["Queued"], [value.title for value in pending])
        self.assertEqual(
            "Persist the complete normalized item.", pending[0].summary
        )

    def test_success_watermark_skips_old_backlog_but_keeps_new_discovery(self):
        old_selected = _item(
            "Old selected", "https://example.test/old-selected", "fixture", 100
        )
        old_unselected = _item(
            "Old unselected", "https://example.test/old-unselected", "fixture", 99
        )
        new_item = _item(
            "New discovery", "https://example.test/new", "fixture", 101
        )
        self.store.upsert_discovered(
            [old_selected, old_unselected], now=1_000
        )

        self.store.mark_emitted([old_selected.uid], emitted_at=1_001)
        self.store.mark_collection_complete(1_000)
        self.store.upsert_discovered([new_item], now=1_002)
        self.store.upsert_discovered([old_unselected], now=1_003)

        self.assertEqual(1_000, self.store.last_completed_at())
        self.assertEqual(
            ["New discovery"],
            [item.title for item in self.store.pending_items(0)],
        )
        self.assertFalse(self.store.is_emitted(old_unselected.uid))

    def test_success_watermark_survives_reopen(self):
        self.store.mark_collection_complete(1_234.5)
        _close_store(self.store)

        self.store = news_digest.StateDB(self.db_path)

        self.assertEqual(1_234.5, self.store.last_completed_at())

    def test_inferred_date_is_stable_across_rediscovery(self):
        first = dataclasses.replace(
            _item("Undated", "https://example.test/undated", "fixture", 100),
            date_inferred=True,
        )
        self.store.upsert_discovered([first])
        rediscovered = dataclasses.replace(first, published_at=200)

        self.store.stabilize_inferred_dates([rediscovered])

        self.assertEqual(100, rediscovered.published_at)


class HuggingFaceAdapterTests(unittest.TestCase):
    def test_trending_uses_current_num_parameters_and_filters_non_models(self):
        payload = {
            "recentlyTrending": [
                {
                    "repoType": "model",
                    "repoData": {
                        "id": "example/LargeModel",
                        "author": "example",
                        "repoType": "model",
                        "numParameters": 7_200_000_000,
                        "downloads": 1234,
                        "likes": 45,
                        "pipeline_tag": "text-generation",
                        "authorData": {"name": "Example Org", "type": "org"},
                    }
                },
                {
                    "repoType": "space",
                    "repoData": {
                        "id": "example/DemoSpace",
                        "repoType": "space",
                        "numParameters": 99_000_000_000,
                        "downloads": 999,
                    }
                },
            ]
        }

        items = news_digest.parse_hf_trending(payload)

        self.assertEqual(1, len(items))
        item = items[0]
        self.assertEqual("example/LargeModel", _value(item, "title"))
        self.assertEqual(
            "https://huggingface.co/example/LargeModel", _value(item, "url")
        )
        self.assertIn("7.2B", _value(item, "summary"))
        self.assertIn("1,234", _value(item, "summary"))
        self.assertNotIn("24h", _value(item, "summary").lower())
        self.assertEqual(["example"], list(_value(item, "authors")))

    def test_daily_paper_reads_nested_upvotes(self):
        payload = [
            {
                "paper": {
                    "id": "2607.12345",
                    "title": "Nested Votes",
                    "summary": "A synthetic paper.",
                    "upvotes": 87,
                    "publishedAt": "2026-07-29T00:00:00.000Z",
                    "authors": [{"name": "One"}, {"name": "Two"}],
                    "organization": {
                        "name": "Example Institute",
                        "fullname": "Example Institute",
                    },
                },
                "upvotes": 3,
            }
        ]

        items = news_digest.parse_hf_daily_papers(payload)

        self.assertEqual(1, len(items))
        self.assertIn("87", _value(items[0], "summary"))
        self.assertNotIn("3 upvotes", _value(items[0], "summary"))
        self.assertEqual(87, _value(items[0], "engagement"))
        self.assertEqual(2, len(_value(items[0], "authors")))
        self.assertEqual(
            "Example Institute", _value(items[0], "affiliation")
        )


class GitHubReleaseAdapterTests(unittest.TestCase):
    def test_pytorch_uses_release_api_and_skips_drafts(self):
        payload = [
            {
                "tag_name": "v2.9.0",
                "html_url": "https://github.com/pytorch/pytorch/releases/tag/v2.9.0",
                "published_at": "2026-07-29T12:00:00Z",
                "body": "Stable compiler and distributed improvements.",
                "draft": False,
                "prerelease": False,
                "author": {"login": "pytorch-bot"},
            },
            {
                "tag_name": "internal-draft",
                "html_url": "https://github.com/pytorch/pytorch/releases/tag/internal",
                "published_at": "2026-07-29T13:00:00Z",
                "draft": True,
            },
        ]

        items = news_digest.parse_pytorch_releases(payload)

        self.assertEqual(1, len(items))
        self.assertEqual("v2.9.0", items[0].title)
        self.assertIn("distributed improvements", items[0].summary)
        self.assertEqual(("pytorch-bot",), items[0].authors)


@unittest.skipUnless(
    hasattr(news_digest, "expand_x_urls"),
    "The collector does not expose an X URL expansion helper",
)
class XUrlExpansionTests(unittest.TestCase):
    def test_link_only_post_keeps_expanded_destination(self):
        entities = {
            "urls": [
                {
                    "url": "https://t.co/abc",
                    "expanded_url": "https://example.test/full/article",
                    "display_url": "example.test/full/article",
                }
            ]
        }

        expanded = news_digest.expand_x_urls("https://t.co/abc", entities)

        self.assertIn("https://example.test/full/article", expanded)
        self.assertNotIn("https://t.co/abc", expanded)
        self.assertTrue(expanded.strip())

    def test_media_only_x_post_is_not_useful_to_text_digest(self):
        self.assertTrue(
            news_digest.is_x_media_only(
                "https://x.com/example/status/123/video/1"
            )
        )
        self.assertTrue(
            news_digest.is_x_media_only(
                "https://twitter.com/example/status/123/photo/1"
            )
        )
        self.assertFalse(
            news_digest.is_x_media_only("https://example.test/full/article")
        )
        self.assertFalse(
            news_digest.is_x_media_only(
                "New model release https://x.com/example/status/123/video/1"
            )
        )

    def test_partial_account_failure_is_reported_degraded(self):
        good_item = _item(
            "Good X item", "https://x.com/i/web/status/1", "X @good", 100
        )

        def fake_fetch(_name, user_id, _category, _token):
            if user_id == "bad-id":
                raise news_digest.FetchError("injected failure")
            return [good_item], ""

        with mock.patch.object(
            news_digest, "_fetch_x_account", side_effect=fake_fetch
        ):
            items, status = news_digest._fetch_x_accounts(
                "X fixture",
                {"good": "good-id", "bad": "bad-id"},
                "🔥 Key People",
                {"x_bearer_token": "fixture-token"},
            )

        self.assertEqual(1, len(items))
        self.assertFalse(status.ok)
        self.assertIn("bad", status.error)


if __name__ == "__main__":
    unittest.main()
