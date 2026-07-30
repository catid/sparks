# Transactional news digest

`news_digest.py` collects and ranks recent items. `digest-poster.py` renders
the result for Slack and, only when explicitly requested, delivers it through
Slack's API.

The collector is deterministic and model-free:

```text
bounded concurrent fetch
  -> RSS 1.0/RSS 2.0/Atom and JSON/X adapters
  -> canonical IDs and SQLite pending queue
  -> freshness ranking with hard per-source diversity
  -> selected-only ArXiv enrichment
  -> escaped Slack overview and threaded sections
  -> emitted commit after each confirmed section
  -> successful-run watermark after the complete digest
```

Python 3.9 or newer is required. A dry run performs real collection and
records discovered items as pending, but makes no external post and does not
mark anything emitted:

```bash
/opt/homebrew/bin/python3 digest-poster.py \
  --fresh-hours 72 \
  --max-per-category 4 \
  --max-per-source 2 \
  --x-max-per-source 1
```

Live delivery requires the bot token in the process environment and a stable
Slack channel ID:

```bash
/opt/homebrew/bin/python3 digest-poster.py \
  --post \
  --channel C0123456789 \
  --fresh-hours 72 \
  --max-per-category 4 \
  --max-per-source 2 \
  --x-max-per-source 1
```

Prefer setting `SLACK_BOT_TOKEN` in OpenClaw's owner-only runtime dotenv and
`NEWS_DIGEST_SLACK_CHANNEL` alongside it instead of placing either value in a
cron payload. Never commit the dotenv, X credentials, SQLite state, generated
output, or installation backups.

The poster creates one root overview and posts category sections as replies.
It marks the item IDs in a section emitted only after every message for that
section succeeds. Stable `client_msg_id` values make a retried Slack chunk
idempotent. Runs use a non-blocking file lock, so an overlapping manual or
scheduled invocation fails rather than racing SQLite state. Failures are
reported as sanitized JSON on stderr and return a nonzero status.

SQLite stores the complete normalized payload, not only a URL hash. An item
that disappears from a source after a failed delivery therefore remains
eligible until it is emitted or ages out. Undated feed entries retain their
original `first_seen` timestamp instead of becoming new again on every poll.
After every section succeeds, a collection watermark acknowledges the whole
candidate window. This prevents later runs from draining lower-ranked
leftovers from the same poll while still retaining the entire window for a
retry when delivery fails. A successful run with no selected items advances
the watermark without posting an empty Slack message.
Do not import the legacy `.seen_urls.json`: older versions marked thousands
of unrendered items seen before delivery.

When scheduling the `--post` form through OpenClaw, disable the cron job's own
message delivery. Otherwise OpenClaw may post the poster's JSON status as a
second message. The poster is the sole Slack delivery owner.

Example cron payload:

```bash
openclaw cron edit JOB_ID \
  --command-argv '[
    "/opt/homebrew/bin/python3",
    "/Users/USER/.openclaw/workspace/news-digest/digest-poster.py",
    "--post",
    "--channel", "C0123456789",
    "--fresh-hours", "72",
    "--max-per-category", "4",
    "--max-per-source", "2",
    "--x-max-per-source", "1"
  ]' \
  --no-deliver \
  --timeout-seconds 300 \
  --failure-alert \
  --failure-alert-after 1
```

Set the failure-alert destination for the local Slack installation. The
gateway daemon must receive `SLACK_BOT_TOKEN` from its owner-only
`~/.openclaw/.env`.

## Sources and credentials

Regular feeds are fetched once per run with bounded concurrency. Current
adapters cover RSS 1.0/RDF, RSS 2.0, Atom, Hugging Face Trending and Daily
Papers, the official PyTorch GitHub Releases API, X curated accounts, and the
authenticated X home timeline. X profile data is requested with the tweet
request rather than once per tweet, and shortened links are expanded. Replies
and textless X photo/video posts are excluded.

The optional private `.x_creds.json` is loaded beside the deployed scripts,
or from `NEWS_DIGEST_X_CREDS_PATH`. It must be a regular mode-`0600` file and
may contain:

```json
{
  "x_bearer_token": "...",
  "x_consumer_key": "...",
  "x_consumer_secret": "...",
  "x_access_token": "...",
  "x_access_secret": "...",
  "x_user_id": "..."
}
```

Never copy real values into this repository. Feed and social content is
treated as untrusted text and escaped before Slack rendering.

## Installation

Run the installer from this repository on the OpenClaw host:

```bash
./install-news-digest.sh
```

It validates all Python entry points before changing the workspace, backs up
existing deployed scripts under
`~/.openclaw/workspace/news-digest/.backups/`, copies only the maintained
code and this README, and tightens known X credential files to mode `0600`.
It never copies repository state or credentials. Use `--dest` for a
nonstandard workspace and `--python` to validate with the interpreter used by
the scheduled job.

Run all deterministic regression tests before installation:

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
shellcheck install-news-digest.sh
```
