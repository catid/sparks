#!/usr/bin/env python3
"""Wait for bounded, content-free readiness from Cerberus voice dependencies."""

from __future__ import annotations

import argparse
import signal
import threading
import time

from voice_bridge import Settings, StatusPublisher, probe_dependency


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("dependency", choices=("asr", "openclaw", "tts", "all"))
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.timeout <= 600:
        raise SystemExit("readiness timeout must be between 1 and 600 seconds")
    settings = Settings.from_environment()
    dependencies = (
        StatusPublisher.DEPENDENCY_NAMES
        if args.dependency == "all"
        else (args.dependency,)
    )
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_args: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_args: stop_event.set())
    deadline = time.monotonic() + args.timeout
    failures: dict[str, str] = {}
    while not stop_event.is_set():
        failures.clear()
        for dependency in dependencies:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                probe_dependency(
                    settings,
                    dependency,
                    timeout=min(2.0, remaining),
                    cancel_event=stop_event,
                )
            except InterruptedError:
                break
            except Exception as error:
                failures[dependency] = StatusPublisher._safe_error_type(error)
        if not failures and not stop_event.is_set() and time.monotonic() < deadline:
            print(f"Voice dependency ready: {args.dependency}", flush=True)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        stop_event.wait(min(0.5, remaining))
    if stop_event.is_set():
        raise SystemExit("readiness wait interrupted")
    summary = ", ".join(
        f"{name}:{failures.get(name, 'TimeoutError')}" for name in dependencies
    )
    raise SystemExit(f"voice dependency readiness timed out ({summary})")


if __name__ == "__main__":
    main()
