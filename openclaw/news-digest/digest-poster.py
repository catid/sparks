#!/usr/bin/env python3
"""Collect a news digest and optionally deliver it directly to Slack.

Dry output is the default.  Passing ``--post`` performs explicit Slack API
calls and records an item as emitted only after its complete section has been
accepted by Slack.
"""

import argparse
import contextlib
import fcntl
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Iterator, List, Optional, Sequence, Tuple

from news_digest import (
    StateDB,
    collect_digest,
    render_slack_overview,
    render_slack_sections,
)


SCRIPT_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_STATE_DB = os.path.join(SCRIPT_DIR, ".news_digest.sqlite3")
SLACK_API_URL = "https://slack.com/api/chat.postMessage"
SLACK_TEXT_LIMIT = 3800


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


def deliver_digest(
    digest: object,
    state_db_path: str,
    channel: str,
    client: SlackClient,
) -> Tuple[str, int]:
    """Post one overview plus section replies and transactionally emit items."""

    sections = render_slack_sections(digest)
    if not sections:
        with StateDB(state_db_path) as state:
            state.mark_collection_complete(
                float(getattr(digest, "generated_at", time.time()))
            )
        return "", 0

    overview = render_slack_overview(digest).strip()
    if not overview:
        raise DeliveryError("digest overview is empty")
    overview_chunks = split_slack_text(overview)
    if len(overview_chunks) != 1:
        raise DeliveryError("digest overview exceeds the Slack message limit")
    all_uids = sorted(
        str(uid)
        for _category, _text, uids in sections
        for uid in uids
        if uid
    )
    root_client_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        "sparks-news-digest:root:" + ",".join(all_uids),
    ))
    root_ts = client.post_message(
        channel, overview_chunks[0], client_msg_id=root_client_id,
    )

    posted_sections = 0
    for category, text, uids in sections:
        chunks = split_slack_text(text)
        if not chunks:
            continue
        normalized_uids = [str(uid) for uid in uids if uid]
        for index, chunk in enumerate(chunks):
            chunk_client_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                "sparks-news-digest:section:%s:%s:%d"
                % (category, ",".join(sorted(normalized_uids)), index),
            ))
            client.post_message(
                channel,
                chunk,
                thread_ts=root_ts,
                client_msg_id=chunk_client_id,
            )
        if normalized_uids:
            with StateDB(state_db_path) as state:
                state.mark_emitted(normalized_uids)
        posted_sections += 1
    with StateDB(state_db_path) as state:
        state.mark_collection_complete(
            float(getattr(digest, "generated_at", time.time()))
        )
    return root_ts, posted_sections


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

        if not args.post:
            _print_dry_run(
                render_slack_overview(digest),
                render_slack_sections(digest),
            )
            return 0

        client = SlackClient(token=token, timeout=args.slack_timeout)
        root_ts, section_count = deliver_digest(
            digest=digest,
            state_db_path=state_db_path,
            channel=args.channel,
            client=client,
        )
        result = {
            "ok": True,
            "posted_sections": section_count,
            "root_ts": root_ts,
            "selected_count": int(getattr(digest, "selected_count", 0)),
        }
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
