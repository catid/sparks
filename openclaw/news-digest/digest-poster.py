#!/usr/bin/env python3
"""Collect a news digest and optionally deliver it directly to Slack.

Dry output is the default.  Passing ``--post`` performs explicit Slack API
calls and records an item as emitted only after its complete section has been
accepted by Slack.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from news_digest import (
    StateDB,
    collect_digest,
    render_slack_overview,
    render_slack_sections,
)
from news_briefing import (
    DEFAULT_PRIORITY_GUIDANCE,
    fallback_plan,
    render_priority_overview,
    render_priority_sections,
    request_priority_plan,
)


SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_STATE_DB = os.path.join(SCRIPT_DIR, ".news_digest.sqlite3")
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_TEXT_LIMIT = 3800
DELIVERY_MANIFEST_VERSION = 2


class PosterError(Exception):
    """A safe, user-facing poster failure."""

    exit_code = 1
    kind = "poster_error"


class ConfigurationError(PosterError):
    exit_code = 2
    kind = "configuration_error"


class AlreadyRunningError(PosterError):
    exit_code = 3
    kind = "already_running"


class CollectionError(PosterError):
    exit_code = 4
    kind = "collection_error"


class DeliveryError(PosterError):
    exit_code = 5
    kind = "delivery_error"


class SafeArgumentParser(argparse.ArgumentParser):
    """Avoid reflecting arbitrary command-line values in scheduled-job logs."""

    def error(self, _message: str) -> None:
        raise ConfigurationError("invalid command-line arguments")


def _nonnegative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least one")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = SafeArgumentParser(
        description=(
            "Collect a ranked news digest. Print Slack mrkdwn by default; "
            "use --post for direct, transactional Slack delivery."
        )
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="post to Slack instead of printing a dry-run rendering",
    )
    parser.add_argument(
        "--channel",
        default=os.environ.get("NEWS_DIGEST_SLACK_CHANNEL"),
        help="Slack channel ID (or NEWS_DIGEST_SLACK_CHANNEL)",
    )
    parser.add_argument(
        "--state-db",
        default=os.environ.get("NEWS_DIGEST_STATE_DB", DEFAULT_STATE_DB),
        help="SQLite discovery/emission state path",
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("NEWS_DIGEST_LOCK_FILE"),
        help="overlap lock path (default: STATE_DB.lock)",
    )
    parser.add_argument("--fresh-hours", type=_nonnegative_int, default=72)
    parser.add_argument("--max-per-category", type=_positive_int, default=8)
    parser.add_argument("--max-per-source", type=_positive_int, default=2)
    parser.add_argument("--x-max-per-source", type=_positive_int, default=1)
    parser.add_argument(
        "--prioritize",
        action="store_true",
        help="ask OpenClaw to summarize and globally prioritize selected items",
    )
    parser.add_argument(
        "--openclaw-bin",
        default=os.environ.get(
            "NEWS_DIGEST_OPENCLAW_BIN", "/opt/homebrew/bin/openclaw"
        ),
        help="OpenClaw executable used by --prioritize",
    )
    parser.add_argument(
        "--priority-model",
        default=os.environ.get(
            "NEWS_DIGEST_PRIORITY_MODEL", "vllm/deepseek-v4-flash"
        ),
        help="OpenClaw model used by --prioritize",
    )
    parser.add_argument(
        "--priority-thinking",
        choices=("off", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=os.environ.get("NEWS_DIGEST_PRIORITY_THINKING", "max"),
    )
    parser.add_argument(
        "--priority-timeout",
        type=_positive_int,
        default=int(os.environ.get("NEWS_DIGEST_PRIORITY_TIMEOUT", "900")),
        help="maximum seconds allowed for OpenClaw prioritization",
    )
    parser.add_argument(
        "--priority-guidance",
        default=os.environ.get(
            "NEWS_DIGEST_PRIORITY_GUIDANCE", DEFAULT_PRIORITY_GUIDANCE
        ),
        help="user-interest guidance for OpenClaw (prefer the environment)",
    )
    parser.add_argument(
        "--slack-timeout",
        type=_positive_int,
        default=30,
        help="seconds allowed for each Slack API call",
    )
    return parser


@contextlib.contextmanager
def exclusive_lock(path: str) -> Iterator[None]:
    """Hold a non-blocking, owner-only process lock for one complete run."""

    absolute = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(absolute)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(absolute, flags, 0o600)
    except OSError as exc:
        raise ConfigurationError("cannot open the overlap lock") from exc

    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AlreadyRunningError("another digest run holds the lock") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, ("%d\n" % os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)


def split_slack_text(text: str, limit: int = SLACK_TEXT_LIMIT) -> List[str]:
    """Split mrkdwn without dropping content, preferring paragraph boundaries."""

    stripped = text.strip()
    if not stripped:
        return []
    chunks = []
    remaining = stripped
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 3:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 3:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut < 1:
            cut = limit
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class SlackClient:
    """Small stdlib Slack client with deliberately sanitized failures."""

    def __init__(
        self,
        token: str,
        timeout: int = 30,
        api_url: str = SLACK_API_URL,
        retry_attempts: int = 4,
    ) -> None:
        if not token:
            raise ConfigurationError("SLACK_BOT_TOKEN is required with --post")
        self._token = token
        self._timeout = timeout
        self._api_url = api_url
        self._retry_attempts = max(1, retry_attempts)

    def post_message(
        self,
        channel: str,
        text: str,
        thread_ts: Optional[str] = None,
        client_msg_id: Optional[str] = None,
    ) -> str:
        payload = {
            "channel": channel,
            "text": text,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if client_msg_id:
            payload["client_msg_id"] = client_msg_id
        request = urllib.request.Request(
            self._api_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "openclaw-news-digest/2",
            },
        )
        for attempt in range(self._retry_attempts):
            try:
                with urllib.request.urlopen(
                    request, timeout=self._timeout
                ) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt + 1 < self._retry_attempts:
                    retry_after = (
                        exc.headers.get("Retry-After", "1")
                        if exc.headers is not None
                        else "1"
                    )
                    try:
                        delay = int(retry_after)
                    except (TypeError, ValueError):
                        delay = 1
                    time.sleep(max(1, min(delay, 30)))
                    continue
                raise DeliveryError(
                    "Slack rejected a message with HTTP status %d" % exc.code
                ) from exc
            except urllib.error.URLError as exc:
                reason = exc.reason
                if isinstance(reason, socket.timeout):
                    detail = "Slack request timed out"
                else:
                    detail = "Slack API could not be reached"
                raise DeliveryError(detail) from exc
            except (socket.timeout, TimeoutError) as exc:
                raise DeliveryError("Slack request timed out") from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeliveryError("Slack returned an invalid response") from exc

            if isinstance(result, dict) and result.get("ok") is True:
                timestamp = result.get("ts")
                if not isinstance(timestamp, str) or not timestamp:
                    raise DeliveryError(
                        "Slack accepted a message without a timestamp"
                    )
                return timestamp

            error_name = result.get("error") if isinstance(result, dict) else None
            if not isinstance(error_name, str) or not error_name:
                error_name = "unknown_error"
            if (
                error_name in ("ratelimited", "rate_limited")
                and attempt + 1 < self._retry_attempts
            ):
                time.sleep(min(2 ** attempt, 8))
                continue
            safe_name = "".join(
                character
                for character in error_name[:80]
                if character.isalnum() or character in "_-"
            )
            raise DeliveryError("Slack API error: %s" % (safe_name or "unknown_error"))

        raise DeliveryError("Slack rate limit retry budget was exhausted")


def _print_dry_run(overview: str, sections: Sequence[Tuple[str, str, list]]) -> None:
    pieces = [overview.strip()]
    pieces.extend(text.strip() for _category, text, _uids in sections if text.strip())
    sys.stdout.write("\n\n".join(piece for piece in pieces if piece))
    sys.stdout.write("\n")


def compile_delivery_manifest(
    digest: object,
    channel: str,
    root_text: str,
    sections: Sequence[Tuple[str, str, list]],
    briefing_source: str,
) -> Tuple[str, Dict[str, Any]]:
    """Freeze exact Slack text and retry identities before the first API call."""

    normalized_root = root_text.strip()
    if not normalized_root or len(normalized_root) > SLACK_TEXT_LIMIT:
        raise DeliveryError("digest overview exceeds the Slack message limit")
    expected_uids = [
        str(item.uid)
        for items in getattr(digest, "sections", {}).values()
        for item in items
        if getattr(item, "uid", "")
    ]
    if len(expected_uids) != len(set(expected_uids)):
        raise DeliveryError("digest contains duplicate item IDs")

    plain_sections = []
    actual_uids = []
    for label, text, uids in sections:
        normalized_uids = [str(uid) for uid in uids if isinstance(uid, str) and uid]
        if len(normalized_uids) != len(uids) or len(normalized_uids) != len(
            set(normalized_uids)
        ):
            raise DeliveryError("digest section item IDs are invalid")
        chunks = split_slack_text(str(text), limit=SLACK_TEXT_LIMIT)
        if not chunks:
            continue
        if any(len(chunk) > SLACK_TEXT_LIMIT for chunk in chunks):
            raise DeliveryError("digest section exceeds the Slack message limit")
        actual_uids.extend(normalized_uids)
        plain_sections.append({
            "label": str(label),
            "uids": normalized_uids,
            "chunks": chunks,
        })
    if (
        len(actual_uids) != len(set(actual_uids))
        or set(actual_uids) != set(expected_uids)
    ):
        raise DeliveryError("rendered digest membership does not match selection")
    if not plain_sections:
        raise DeliveryError("rendered digest has no sections")

    seed = {
        "version": DELIVERY_MANIFEST_VERSION,
        "channel": str(channel),
        "generated_at": float(getattr(digest, "generated_at", time.time())),
        "briefing_source": str(briefing_source),
        "root_text": normalized_root,
        "sections": plain_sections,
    }
    seed_text = json.dumps(
        seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    delivery_key = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    root_client_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "sparks-news-digest:v2:%s:root" % delivery_key,
    ))
    rendered_sections = []
    for section_index, section in enumerate(plain_sections):
        rendered_chunks = []
        for chunk_index, chunk in enumerate(section["chunks"]):
            rendered_chunks.append({
                "text": chunk,
                "client_msg_id": str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "sparks-news-digest:v2:%s:section:%d:chunk:%d"
                    % (delivery_key, section_index, chunk_index),
                )),
            })
        rendered_sections.append({
            "label": section["label"],
            "uids": section["uids"],
            "chunks": rendered_chunks,
        })
    manifest = {
        "version": DELIVERY_MANIFEST_VERSION,
        "channel": str(channel),
        "generated_at": seed["generated_at"],
        "selected_count": len(actual_uids),
        "briefing_source": str(briefing_source),
        "root": {
            "text": normalized_root,
            "client_msg_id": root_client_id,
        },
        "sections": rendered_sections,
    }
    return delivery_key, manifest


def _validate_delivery_record(
    record: Mapping[str, Any],
    channel: str,
) -> Dict[str, Any]:
    delivery_key = record.get("delivery_key")
    manifest = record.get("manifest")
    if (
        not isinstance(delivery_key, str)
        or len(delivery_key) != 64
        or not isinstance(manifest, dict)
        or manifest.get("version") != DELIVERY_MANIFEST_VERSION
        or manifest.get("channel") != channel
    ):
        raise DeliveryError("active delivery state is invalid")
    root = manifest.get("root")
    sections = manifest.get("sections")
    if not isinstance(root, dict) or not isinstance(sections, list) or not sections:
        raise DeliveryError("active delivery state is invalid")
    root_text = root.get("text")
    root_client_id = root.get("client_msg_id")
    expected_root_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "sparks-news-digest:v2:%s:root" % delivery_key,
    ))
    if (
        not isinstance(root_text, str)
        or not root_text
        or len(root_text) > SLACK_TEXT_LIMIT
        or root_client_id != expected_root_id
    ):
        raise DeliveryError("active delivery root is invalid")

    seen_uids = set()
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise DeliveryError("active delivery section is invalid")
        uids = section.get("uids")
        chunks = section.get("chunks")
        if not isinstance(uids, list) or not isinstance(chunks, list) or not chunks:
            raise DeliveryError("active delivery section is invalid")
        if any(not isinstance(uid, str) or not uid for uid in uids):
            raise DeliveryError("active delivery section item IDs are invalid")
        if len(uids) != len(set(uids)) or seen_uids.intersection(uids):
            raise DeliveryError("active delivery section item IDs are invalid")
        seen_uids.update(uids)
        for chunk_index, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise DeliveryError("active delivery chunk is invalid")
            text = chunk.get("text")
            client_msg_id = chunk.get("client_msg_id")
            expected_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                "sparks-news-digest:v2:%s:section:%d:chunk:%d"
                % (delivery_key, section_index, chunk_index),
            ))
            if (
                not isinstance(text, str)
                or not text
                or len(text) > SLACK_TEXT_LIMIT
                or client_msg_id != expected_id
            ):
                raise DeliveryError("active delivery chunk is invalid")
    if manifest.get("selected_count") != len(seen_uids):
        raise DeliveryError("active delivery count is invalid")
    completed = record.get("completed_sections")
    if (
        not isinstance(completed, list)
        or any(index not in range(len(sections)) for index in completed)
    ):
        raise DeliveryError("active delivery progress is invalid")
    return manifest


def deliver_manifest(
    record: Mapping[str, Any],
    state_db_path: str,
    channel: str,
    client: SlackClient,
) -> Tuple[str, int]:
    """Resume and finish one immutable delivery manifest."""

    manifest = _validate_delivery_record(record, channel)
    delivery_key = str(record["delivery_key"])
    root_ts = str(record.get("root_ts") or "")
    if not root_ts:
        root = manifest["root"]
        root_ts = client.post_message(
            channel,
            root["text"],
            client_msg_id=root["client_msg_id"],
        )
        try:
            with StateDB(state_db_path) as state:
                state.set_delivery_root_ts(delivery_key, root_ts)
        except (OSError, ValueError) as exc:
            raise DeliveryError("delivery state could not save the root") from exc

    completed = set(record.get("completed_sections") or [])
    sections = manifest["sections"]
    for section_index, section in enumerate(sections):
        if section_index in completed:
            continue
        for chunk in section["chunks"]:
            client.post_message(
                channel,
                chunk["text"],
                thread_ts=root_ts,
                client_msg_id=chunk["client_msg_id"],
            )
        try:
            with StateDB(state_db_path) as state:
                state.mark_delivery_section(delivery_key, section_index)
        except (OSError, ValueError) as exc:
            raise DeliveryError("delivery state could not commit a section") from exc
        completed.add(section_index)

    try:
        with StateDB(state_db_path) as state:
            state.complete_delivery(
                delivery_key,
                float(manifest.get("generated_at") or time.time()),
            )
    except (OSError, ValueError) as exc:
        raise DeliveryError("delivery state could not finalize the digest") from exc
    return root_ts, len(sections)


def deliver_digest(
    digest: object,
    state_db_path: str,
    channel: str,
    client: SlackClient,
) -> Tuple[str, int]:
    """Compatibility helper using the deterministic renderer and manifest."""

    sections = render_slack_sections(digest)
    if not sections:
        with StateDB(state_db_path) as state:
            state.mark_collection_complete(
                float(getattr(digest, "generated_at", time.time()))
            )
        return "", 0
    delivery_key, manifest = compile_delivery_manifest(
        digest,
        channel,
        render_slack_overview(digest),
        sections,
        "deterministic",
    )
    try:
        with StateDB(state_db_path) as state:
            state.save_delivery_manifest(delivery_key, channel, manifest)
            record = state.load_active_delivery(channel)
    except (OSError, ValueError) as exc:
        raise DeliveryError("delivery state could not save the digest") from exc
    if record is None:
        raise DeliveryError("delivery state did not retain the digest")
    return deliver_manifest(record, state_db_path, channel, client)


def run(args: argparse.Namespace) -> int:
    if args.post and not args.channel:
        raise ConfigurationError(
            "--channel or NEWS_DIGEST_SLACK_CHANNEL is required with --post"
        )
    token = os.environ.get("SLACK_BOT_TOKEN", "") if args.post else ""
    if args.post and not token:
        raise ConfigurationError("SLACK_BOT_TOKEN is required with --post")

    state_db_path = os.path.abspath(os.path.expanduser(args.state_db))
    lock_path = args.lock_file or state_db_path + ".lock"
    with exclusive_lock(lock_path):
        client = (
            SlackClient(token=token, timeout=args.slack_timeout)
            if args.post
            else None
        )
        if args.post:
            try:
                with StateDB(state_db_path) as state:
                    active = state.load_active_delivery(str(args.channel))
            except (OSError, ValueError) as exc:
                raise DeliveryError("active delivery state could not be read") from exc
            if active is not None:
                manifest = _validate_delivery_record(active, str(args.channel))
                root_ts, section_count = deliver_manifest(
                    active,
                    state_db_path,
                    str(args.channel),
                    client,
                )
                result = {
                    "briefing_source": str(
                        manifest.get("briefing_source") or "unknown"
                    ),
                    "ok": True,
                    "posted_sections": section_count,
                    "resumed": True,
                    "root_ts": root_ts,
                    "selected_count": int(
                        manifest.get("selected_count") or 0
                    ),
                }
                sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
                return 0

        try:
            digest = collect_digest(
                state_db_path=state_db_path,
                fresh_hours=args.fresh_hours,
                max_per_category=args.max_per_category,
                max_per_source=args.max_per_source,
                x_max_per_source=args.x_max_per_source,
                persist_discovery=True,
            )
        except Exception as exc:
            raise CollectionError("digest collection failed") from exc

        if not getattr(digest, "sections", {}):
            if args.post:
                with StateDB(state_db_path) as state:
                    state.mark_collection_complete(
                        float(getattr(digest, "generated_at", time.time()))
                    )
                result = {
                    "briefing_source": "none",
                    "ok": True,
                    "posted_sections": 0,
                    "resumed": False,
                    "root_ts": "",
                    "selected_count": 0,
                }
                sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
            else:
                _print_dry_run(render_slack_overview(digest), [])
            return 0

        diagnostic = ""
        if args.prioritize:
            plan = request_priority_plan(
                digest=digest,
                openclaw_bin=args.openclaw_bin,
                model=args.priority_model,
                thinking=args.priority_thinking,
                timeout=args.priority_timeout,
                guidance=args.priority_guidance,
            )
            root_text = render_priority_overview(digest, plan)
            rendered_sections = render_priority_sections(digest, plan)
            briefing_source = str(plan.source)
            diagnostic = str(getattr(plan, "diagnostic", "") or "")
        else:
            plan = fallback_plan(digest)
            root_text = render_slack_overview(digest)
            rendered_sections = render_slack_sections(digest)
            briefing_source = "deterministic"

        if not args.post:
            _print_dry_run(root_text, rendered_sections)
            return 0

        delivery_key, manifest = compile_delivery_manifest(
            digest,
            str(args.channel),
            root_text,
            rendered_sections,
            briefing_source,
        )
        try:
            with StateDB(state_db_path) as state:
                state.save_delivery_manifest(
                    delivery_key,
                    str(args.channel),
                    manifest,
                )
                active = state.load_active_delivery(str(args.channel))
        except (OSError, ValueError) as exc:
            raise DeliveryError("delivery state could not save the digest") from exc
        if active is None:
            raise DeliveryError("delivery state did not retain the digest")
        root_ts, section_count = deliver_manifest(
            active,
            state_db_path,
            str(args.channel),
            client,
        )
        result = {
            "briefing_source": briefing_source,
            "ok": True,
            "posted_sections": section_count,
            "resumed": False,
            "root_ts": root_ts,
            "selected_count": int(getattr(digest, "selected_count", 0)),
        }
        if diagnostic:
            result["briefing_diagnostic"] = diagnostic
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return run(args)
    except PosterError as exc:
        failure = {
            "error": exc.kind,
            "message": str(exc),
            "ok": False,
        }
        sys.stderr.write(json.dumps(failure, sort_keys=True) + "\n")
        return exc.exit_code
    except Exception:
        failure = {
            "error": "unexpected_error",
            "message": "digest poster failed unexpectedly",
            "ok": False,
        }
        sys.stderr.write(json.dumps(failure, sort_keys=True) + "\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
