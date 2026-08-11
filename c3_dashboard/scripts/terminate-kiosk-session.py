#!/usr/bin/env python3
"""Safely terminate the PAM/logind session created for the C3 kiosk.

``PAMName=login`` is required so rootless Xorg receives the active VT's DRM
lease.  pam_systemd consequently moves the kiosk process tree from the service
cgroup into a login session scope.  systemd therefore cannot contain that tree
with the service's normal ``KillMode=control-group`` operation.

The installer copies this helper to a root-owned libexec path.  It accepts only
the rendered service identity, reads a tightly validated session ID from the
systemd-owned runtime directory, verifies the logind session identity, and
then terminates that one session scope.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable


SESSION_FILE = Path("/run/dgx-spark-c3-kiosk/session-id")
LOGINCTL = "/usr/bin/loginctl"
SYSTEMCTL = "/usr/bin/systemctl"
COMMAND_TIMEOUT_SECONDS = 5


class CleanupError(RuntimeError):
    """The cleanup target did not satisfy the kiosk safety contract."""


def read_session_id(path: Path, expected_uid: int) -> str | None:
    """Read a private, regular session-ID file without following symlinks."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CleanupError(f"cannot safely open {path}: {exc}") from exc

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise CleanupError(f"{path} is not a regular file")
        if metadata.st_uid != expected_uid:
            raise CleanupError(f"{path} is not owned by UID {expected_uid}")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise CleanupError(f"{path} permissions are broader than 0600")
        payload = os.read(descriptor, 65)
        if len(payload) > 64:
            raise CleanupError(f"{path} is unexpectedly large")
    finally:
        os.close(descriptor)

    try:
        session_id = payload.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise CleanupError(f"{path} is not ASCII") from exc
    if not session_id or not session_id.isdecimal() or len(session_id) > 20:
        raise CleanupError(f"{path} does not contain a numeric session ID")
    return session_id


def session_id_from_main_pid(
    main_pid: int, expected_uid: int, proc_root: Path = Path("/proc")
) -> str:
    """Resolve the login session for a tracked pre-upgrade service leader."""
    if main_pid <= 1:
        raise CleanupError("the fallback main PID is invalid")
    process_root = proc_root / str(main_pid)
    try:
        status = (process_root / "status").read_text(encoding="ascii")
        session_id = (process_root / "sessionid").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise CleanupError(f"cannot inspect fallback main PID {main_pid}: {exc}") from exc
    uid_line = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    try:
        real_uid, effective_uid, *_rest = (
            int(value) for value in uid_line.removeprefix("Uid:").split()
        )
    except ValueError as exc:
        raise CleanupError(f"cannot parse UID for fallback main PID {main_pid}") from exc
    if real_uid != expected_uid or effective_uid != expected_uid:
        raise CleanupError(
            f"fallback main PID {main_pid} is not owned by UID {expected_uid}"
        )
    if not session_id.isdecimal() or not 1 <= len(session_id) <= 20:
        raise CleanupError(f"fallback main PID {main_pid} has no numeric session ID")
    return session_id


Runner = Callable[..., subprocess.CompletedProcess[str]]


def session_property(session_id: str, name: str, runner: Runner) -> str | None:
    """Return one logind property, or None when the session has gone away."""
    try:
        result = runner(
            [LOGINCTL, "show-session", session_id, f"--property={name}", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"cannot query logind session {session_id}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        if "no session" in detail.lower() and "known" in detail.lower():
            return None
        raise CleanupError(
            f"cannot query logind session {session_id}: "
            f"{detail or f'exit status {result.returncode}'}"
        )
    return result.stdout.strip()


def validate_session(
    session_id: str,
    expected_user: str,
    expected_uid: int,
    expected_tty: str,
    runner: Runner = subprocess.run,
    expected_leader: int | None = None,
) -> bool:
    """Require the exact local login identity used by the kiosk unit."""
    properties = {
        name: session_property(session_id, name, runner)
        for name in ("Name", "User", "Service", "TTY", "Remote")
    }
    if any(value is None for value in properties.values()):
        return False
    expected = {
        "Name": expected_user,
        "User": str(expected_uid),
        "Service": "login",
        "TTY": expected_tty,
        "Remote": "no",
    }
    if properties != expected:
        observed = ", ".join(f"{key}={value!r}" for key, value in properties.items())
        raise CleanupError(
            f"refusing to terminate session {session_id}; identity mismatch ({observed})"
        )
    if expected_leader is not None:
        leader = session_property(session_id, "Leader", runner)
        if leader != str(expected_leader):
            raise CleanupError(
                f"refusing to terminate session {session_id}; "
                f"leader {leader!r} is not PID {expected_leader}"
            )
    return True


def terminate_session(session_id: str, runner: Runner = subprocess.run) -> None:
    unit_name = f"session-{session_id}.scope"
    try:
        result = runner(
            [SYSTEMCTL, "stop", unit_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CleanupError(f"cannot stop logind scope {unit_name}: {exc}") from exc
    if result.returncode != 0:
        if session_property(session_id, "Name", runner) is None:
            return
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise CleanupError(f"cannot stop logind scope {unit_name}: {detail}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    parser.add_argument("--uid", required=True, type=int)
    parser.add_argument("--tty", default="tty7")
    parser.add_argument("--main-pid", type=int)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate the target without stopping it",
    )
    args = parser.parse_args(argv)
    if not args.user or not args.user.replace("-", "_").isalnum():
        parser.error("--user must be a simple account name")
    if args.uid <= 0:
        parser.error("--uid must identify an unprivileged account")
    if args.tty != "tty7":
        parser.error("only tty7 is accepted for the C3 kiosk")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        session_id = read_session_id(SESSION_FILE, args.uid)
        if session_id is None:
            if args.main_pid is None:
                return 0
            session_id = session_id_from_main_pid(args.main_pid, args.uid)
            expected_leader = args.main_pid
        else:
            expected_leader = None
        if not validate_session(
            session_id,
            args.user,
            args.uid,
            args.tty,
            expected_leader=expected_leader,
        ):
            # An already-closed session is the expected idempotent path for
            # ExecStopPost after ExecStop has completed.
            return 0
        if args.check_only:
            print(f"Validated C3 kiosk login session {session_id}.", flush=True)
            return 0
        terminate_session(session_id)
        print(f"Stopped C3 kiosk login scope for session {session_id}.", flush=True)
        return 0
    except CleanupError as exc:
        print(f"C3 kiosk session cleanup: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
