# Transactional news digest

`news_digest.py` collects and deterministically shortlists recent items.
`news_briefing.py` can ask OpenClaw's stateless, tool-free model runner to
summarize and prioritize that shortlist. `digest-poster.py` renders the result
for Slack and, only when explicitly requested, delivers it through Slack's
API.

The collector is deterministic and model-free:

```text
bounded concurrent fetch
  -> RSS 1.0/RSS 2.0/Atom and JSON/X adapters
  -> canonical IDs and SQLite pending queue
  -> freshness ranking with hard per-source diversity
  -> selected-only ArXiv enrichment
  -> optional OpenClaw briefing with strict JSON validation
  -> escaped Slack overview and threaded sections
  -> immutable delivery manifest before the first Slack call
  -> emitted commit after each confirmed manifest section
  -> successful-run watermark after the complete digest
```

Python 3.9 or newer is required. A dry run performs real collection and
records discovered items as pending, but makes no external post and does not
mark anything emitted:

```bash
/opt/homebrew/bin/python3 digest-poster.py \
  --prioritize \
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
  --prioritize \
  --fresh-hours 72 \
  --max-per-category 4 \
  --max-per-source 2 \
  --x-max-per-source 1
```

Prefer setting `SLACK_BOT_TOKEN` in OpenClaw's owner-only runtime dotenv and
`NEWS_DIGEST_SLACK_CHANNEL` alongside it instead of placing either value in a
cron payload. Never commit the dotenv, X credentials, SQLite state, generated
output, or installation backups.

With `--prioritize`, the main Slack channel message contains five linked top
items with short reasons plus OpenClaw's concise briefing. The thread lists
every selected item in global priority order with visible `#1`, `#2`, ...
rank numbers. The model may only reorder known IDs and write bounded briefing
prose; canonical titles, links, metadata, membership, and delivery state
remain deterministic.

Before the first Slack API call, the poster stores the exact root text,
thread chunks, IDs, and ordering in an integrity-checked SQLite manifest. It
marks a manifest section emitted only after every message for that section
succeeds. An interrupted run resumes the stored root/thread instead of
collecting again or asking the model for a different answer. Stable
`client_msg_id` values make retried Slack chunks idempotent. Runs use a
non-blocking file lock, so an overlapping invocation fails rather than racing
SQLite state. Failures are sanitized and return a nonzero status.

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

## OpenClaw prioritization

The briefing step calls:

```text
openclaw infer model run --local --model vllm/deepseek-v4-flash \
  --thinking max --prompt ... --json
```

This is OpenClaw's lean one-shot model interface. It reuses the configured
provider and model but exposes no tools, filesystem, shell, Slack actions,
agent memory, or transcript. Feed titles and summaries are labeled untrusted
inside compact JSON. The response must be one strict, versioned JSON object
containing an exact permutation of the selected item references. Unknown,
missing, duplicate, malformed, or oversized model output gets one bounded
schema-repair attempt. A second validation failure, timeout, or process error
uses the deterministic fallback; the chosen result is still frozen into the
delivery manifest. The main channel message visibly labels that fallback.
Both model attempts share the single `--priority-timeout` budget.

The default profile is personalized for a two-DGX-Spark owner running local
DeepSeek/vLLM for OpenClaw: directly useful serving releases, kernels,
quantization, performance, and agent-reliability findings rank first.
Robotics/agent research follows, then substantive AI research, science,
space, and math. General corporate announcements, promotion, repeated
versions of one story, and low-information posts rank lower.
Override it with `NEWS_DIGEST_PRIORITY_GUIDANCE`. Other optional settings are
`NEWS_DIGEST_OPENCLAW_BIN`, `NEWS_DIGEST_PRIORITY_MODEL`,
`NEWS_DIGEST_PRIORITY_THINKING`, and `NEWS_DIGEST_PRIORITY_TIMEOUT`.

Example cron payload:

```bash
openclaw cron edit JOB_ID \
  --command-argv '[
    "/opt/homebrew/bin/python3",
    "/Users/USER/.openclaw/workspace/news-digest/digest-poster.py",
    "--post",
    "--channel", "C0123456789",
    "--prioritize",
    "--priority-thinking", "max",
    "--fresh-hours", "72",
    "--max-per-category", "4",
    "--max-per-source", "2",
    "--x-max-per-source", "1"
  ]' \
  --no-deliver \
  --timeout-seconds 1200 \
  --no-output-timeout-seconds 1000 \
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

It validates all Python modules and entry points before changing the
workspace, backs up existing deployed scripts under
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
