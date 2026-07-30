#!/usr/bin/env python3
"""Guarded patcher for OpenClaw's Slack five-second thinking heartbeat."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


EXPECTED_PACKAGE = "@openclaw/slack"
EXPECTED_VERSION = "2026.7.1"
EXPECTED_UNPATCHED_SHA256 = (
    "b93c2120b970b88d44b6ede42fbd895a9d69210357064172e473c1c85c8cc724"
)
EXPECTED_PATCHED_SHA256 = (
    "20558ccf0a15fc708f011f022171d1df50d6f1296aa76faed269ead7f5abca7c"
)
PATCH_MARKER = "openclaw-slack-thinking-heartbeat-v1"

CONSTANTS_ANCHOR = """const SLACK_THREAD_LOADING_MESSAGES = [
\t"Reading the thread...",
\t"Checking context...",
\t"Working through the request...",
\t"Putting it all together..."
];
function resolveSlackMessageTimestampMs(message) {"""

CONSTANTS_REPLACEMENT = """const SLACK_THREAD_LOADING_MESSAGES = [
\t"Reading the thread...",
\t"Checking context...",
\t"Working through the request...",
\t"Putting it all together..."
];
// openclaw-slack-thinking-heartbeat-v1
const SLACK_THINKING_HEARTBEAT_INTERVAL_MS = 5e3;
const SLACK_THINKING_HEARTBEAT_MAX_DOTS = 3500;
function resolveSlackMessageTimestampMs(message) {"""

DECLARATIONS_ANCHOR = """\tlet hasStreamedMessage = false;
\tconst streamMode = slackStreaming.draftMode;
\tconst useNativeProgressStreaming = useStreaming && slackStreaming.mode === "progress";"""

DECLARATIONS_REPLACEMENT = """\tlet hasStreamedMessage = false;
\tconst streamMode = slackStreaming.draftMode;
\tlet thinkingHeartbeatDots = 1;
\tlet thinkingHeartbeatTimer;
\tconst updateSlackThinkingHeartbeat = () => {
\t\tif (!draftStream || streamMode !== "status_final") return;
\t\tconst dots = ".".repeat(Math.min(thinkingHeartbeatDots, SLACK_THINKING_HEARTBEAT_MAX_DOTS));
\t\tdraftStream.update(`thinking${dots}`);
\t\thasStreamedMessage = true;
\t\tthinkingHeartbeatDots += 1;
\t};
\tconst startSlackThinkingHeartbeat = async () => {
\t\tif (!draftStream || streamMode !== "status_final") return;
\t\tupdateSlackThinkingHeartbeat();
\t\tawait draftStream.flush();
\t\tthinkingHeartbeatTimer = setInterval(updateSlackThinkingHeartbeat, SLACK_THINKING_HEARTBEAT_INTERVAL_MS);
\t\tthinkingHeartbeatTimer.unref?.();
\t};
\tconst stopSlackThinkingHeartbeat = () => {
\t\tif (!thinkingHeartbeatTimer) return;
\t\tclearInterval(thinkingHeartbeatTimer);
\t\tthinkingHeartbeatTimer = void 0;
\t};
\tconst useNativeProgressStreaming = useStreaming && slackStreaming.mode === "progress";"""

TRY_ANCHOR = """\ttry {
\t\tconst turnResult = await dispatchChannelInboundReply({"""

TRY_REPLACEMENT = """\ttry {
\t\tawait startSlackThinkingHeartbeat();
\t\tconst turnResult = await dispatchChannelInboundReply({"""

FINALLY_ANCHOR = """\t} finally {
\t\tprogressDraftGate.cancel();
\t\tawait draftStream?.discardPending();
\t}"""

FINALLY_REPLACEMENT = """\t} finally {
\t\tstopSlackThinkingHeartbeat();
\t\tprogressDraftGate.cancel();
\t\tawait draftStream?.discardPending();
\t}"""

PATCH_PAIRS = (
    (CONSTANTS_ANCHOR, CONSTANTS_REPLACEMENT),
    (DECLARATIONS_ANCHOR, DECLARATIONS_REPLACEMENT),
    (TRY_ANCHOR, TRY_REPLACEMENT),
    (FINALLY_ANCHOR, FINALLY_REPLACEMENT),
)


class PatchError(RuntimeError):
    """A safe, user-facing patch validation failure."""


@dataclass
class PatchPlan:
    plugin_dir: Path
    target: Path
    original: bytes
    patched: bytes
    already_patched: bool
    mode: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label} anchor count is {count}, expected exactly 1; "
            "refusing an unknown adapter layout"
        )
    return text.replace(old, new, 1)


def validate_patched_text(text: str) -> None:
    checks = {
        "patch marker": f"// {PATCH_MARKER}",
        "five-second interval": "SLACK_THINKING_HEARTBEAT_INTERVAL_MS = 5e3",
        "bounded cumulative dots": "SLACK_THINKING_HEARTBEAT_MAX_DOTS = 3500",
        "initial heartbeat flush": "await startSlackThinkingHeartbeat();",
        "timer cleanup": "stopSlackThinkingHeartbeat();",
    }
    for label, needle in checks.items():
        count = text.count(needle)
        if count != 1:
            raise PatchError(
                f"patched {label} count is {count}, expected exactly 1"
            )
    unsafe_error_cleanup = (
        "if (!draftPreviewCommitted) await draftStream?.clear();"
    )
    if unsafe_error_cleanup in text:
        raise PatchError(
            "unsafe generic dispatch-error preview cleanup detected; "
            "it can delete an already finalized answer"
        )


def patch_text(text: str) -> str:
    if PATCH_MARKER in text:
        validate_patched_text(text)
        return text

    partial_needles = (
        "SLACK_THINKING_HEARTBEAT_INTERVAL_MS",
        "startSlackThinkingHeartbeat",
        "stopSlackThinkingHeartbeat",
    )
    if any(needle in text for needle in partial_needles):
        raise PatchError("partial heartbeat patch detected; refusing to continue")

    patched = text
    for index, (old, new) in enumerate(PATCH_PAIRS, start=1):
        patched = replace_once(patched, old, new, f"patch step {index}")
    validate_patched_text(patched)
    return patched


def resolve_node_bin(explicit: Optional[str]) -> Path:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        (
            Path("/opt/homebrew/bin/node"),
            Path("/usr/local/bin/node"),
            Path("/usr/bin/node"),
        )
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise PatchError("node executable not found; use --node-bin")


def run_node_check(node_bin: Path, target: Path) -> None:
    result = subprocess.run(
        (str(node_bin), "--check", str(target)),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        summary = detail[-1] if detail else f"exit {result.returncode}"
        raise PatchError(f"JavaScript syntax check failed for {target}: {summary}")


def require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink():
        raise PatchError(f"{label} must not be a symlink: {path}")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as exc:
        raise PatchError(f"{label} is missing: {path}") from exc
    if not stat.S_ISREG(mode):
        raise PatchError(f"{label} is not a regular file: {path}")


def resolve_plan(plugin_dir_arg: str) -> PatchPlan:
    unresolved = Path(plugin_dir_arg).expanduser()
    if unresolved.is_symlink():
        raise PatchError(f"plugin directory must not be a symlink: {unresolved}")
    try:
        plugin_dir = unresolved.resolve(strict=True)
    except FileNotFoundError as exc:
        raise PatchError(f"plugin directory is missing: {unresolved}") from exc
    if not plugin_dir.is_dir():
        raise PatchError(f"plugin path is not a directory: {plugin_dir}")

    package_path = plugin_dir / "package.json"
    require_regular_file(package_path, "package manifest")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PatchError(f"cannot read package manifest: {package_path}") from exc
    if package.get("name") != EXPECTED_PACKAGE:
        raise PatchError(
            f"unexpected package {package.get('name')!r} at {plugin_dir}"
        )
    if package.get("version") != EXPECTED_VERSION:
        raise PatchError(
            f"unsupported {EXPECTED_PACKAGE} version "
            f"{package.get('version')!r}; expected {EXPECTED_VERSION}"
        )

    targets = sorted((plugin_dir / "dist").glob("pipeline.runtime-*.js"))
    if len(targets) != 1:
        raise PatchError(
            f"found {len(targets)} pipeline runtime bundles under {plugin_dir}; "
            "expected exactly 1"
        )
    target = targets[0]
    require_regular_file(target, "pipeline runtime bundle")
    original = target.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(f"pipeline runtime is not UTF-8: {target}") from exc

    already_patched = PATCH_MARKER in text
    if already_patched:
        digest = sha256_bytes(original)
        if digest != EXPECTED_PATCHED_SHA256:
            raise PatchError(
                f"unrecognized patched bundle SHA-256 {digest} at {target}; "
                f"expected {EXPECTED_PATCHED_SHA256}"
            )
        patched_text = patch_text(text)
    else:
        digest = sha256_bytes(original)
        if digest != EXPECTED_UNPATCHED_SHA256:
            raise PatchError(
                f"unrecognized unpatched bundle SHA-256 {digest} at {target}; "
                f"expected {EXPECTED_UNPATCHED_SHA256}"
            )
        patched_text = patch_text(text)

    return PatchPlan(
        plugin_dir=plugin_dir,
        target=target,
        original=original,
        patched=patched_text.encode("utf-8"),
        already_patched=already_patched,
        mode=stat.S_IMODE(target.stat().st_mode),
    )


def ensure_unique_plans(plans: Sequence[PatchPlan]) -> None:
    seen = set()
    for plan in plans:
        identity = str(plan.target)
        if identity in seen:
            raise PatchError(f"duplicate target: {plan.target}")
        seen.add(identity)


def require_target_unchanged(plan: PatchPlan) -> None:
    require_regular_file(plan.target, "pipeline runtime bundle")
    current = plan.target.read_bytes()
    if current != plan.original:
        raise PatchError(
            f"pipeline runtime changed after preflight: {plan.target}; "
            "refusing to overwrite concurrent plugin work"
        )


def check_candidate_syntax(plan: PatchPlan, node_bin: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{plan.target.name}.heartbeat-check-",
        suffix=".js",
        dir=str(plan.target.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(plan.patched)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, plan.mode)
        run_node_check(node_bin, temporary)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def ensure_backup_root(path_arg: Optional[str]) -> Path:
    root = (
        Path(path_arg).expanduser()
        if path_arg
        else Path.home() / ".openclaw/backups/slack-thinking-heartbeat"
    )
    if root.is_symlink():
        raise PatchError(f"backup root must not be a symlink: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not root.is_dir():
        raise PatchError(f"backup root is not a directory: {root}")
    os.chmod(root, 0o700)
    return root.resolve(strict=True)


def backup_plan(plan: PatchPlan, backup_set: Path) -> Path:
    project_name = next(
        (
            part
            for part in reversed(plan.plugin_dir.parts)
            if part.startswith("openclaw-slack-")
        ),
        "slack-plugin",
    )
    path_tag = hashlib.sha256(str(plan.plugin_dir).encode("utf-8")).hexdigest()[:12]
    destination_dir = backup_set / f"{project_name}-{path_tag}"
    destination_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    destination = destination_dir / plan.target.name
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(plan.original)
        handle.flush()
        os.fsync(handle.fileno())
    return destination


def atomic_replace(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.heartbeat-write-",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def timestamp_slug() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def apply_plans(
    plans: Sequence[PatchPlan],
    node_bin: Path,
    backup_root_arg: Optional[str],
) -> Optional[Path]:
    changed = [plan for plan in plans if not plan.already_patched]
    if not changed:
        return None

    # Validate every candidate before backing up or replacing any target.
    for plan in changed:
        check_candidate_syntax(plan, node_bin)

    backup_root = ensure_backup_root(backup_root_arg)
    backup_set = backup_root / timestamp_slug()
    backup_set.mkdir(mode=0o700, parents=False, exist_ok=False)
    for plan in changed:
        require_target_unchanged(plan)
        backup_plan(plan, backup_set)

    replaced = []
    try:
        for plan in changed:
            require_target_unchanged(plan)
            # Record ownership before replacement so even an error after
            # os.replace (for example a directory fsync failure) rolls back.
            replaced.append(plan)
            atomic_replace(plan.target, plan.patched, plan.mode)
            run_node_check(node_bin, plan.target)
    except Exception:
        rollback_errors = []
        for plan in reversed(replaced):
            try:
                current = plan.target.read_bytes()
                if current == plan.original:
                    continue
                if current != plan.patched:
                    raise PatchError(
                        "target changed during failed transaction; "
                        "refusing to overwrite concurrent work"
                    )
                atomic_replace(plan.target, plan.original, plan.mode)
                run_node_check(node_bin, plan.target)
            except Exception as rollback_error:  # pragma: no cover - emergency path
                rollback_errors.append(f"{plan.target}: {rollback_error}")
        if rollback_errors:
            raise PatchError(
                "patch failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise
    return backup_set


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify or apply the guarded OpenClaw Slack five-second heartbeat"
        )
    )
    parser.add_argument(
        "--plugin-dir",
        action="append",
        required=True,
        help="installed @openclaw/slack package directory; repeatable",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the patch; the default is a read-only installed-state check",
    )
    parser.add_argument(
        "--backup-dir",
        help="owner-only backup root (default: ~/.openclaw/backups/...)",
    )
    parser.add_argument("--node-bin", help="node executable for --check")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        node_bin = resolve_node_bin(args.node_bin)
        plans = [resolve_plan(item) for item in args.plugin_dir]
        ensure_unique_plans(plans)

        # Syntax-check installed patched targets and every proposed candidate.
        for plan in plans:
            if plan.already_patched:
                run_node_check(node_bin, plan.target)

        if not args.apply:
            unpatched = [plan.target for plan in plans if not plan.already_patched]
            if unpatched:
                for target in unpatched:
                    print(f"compatible but UNPATCHED: {target}", file=sys.stderr)
                return 1
            for plan in plans:
                print(f"verified heartbeat patch: {plan.target}")
            return 0

        backup_set = apply_plans(plans, node_bin, args.backup_dir)
        for plan in plans:
            state = "already patched" if plan.already_patched else "patched"
            print(f"{state}: {plan.target}")
        if backup_set:
            print(f"backups: {backup_set}")
        return 0
    except (OSError, PatchError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
