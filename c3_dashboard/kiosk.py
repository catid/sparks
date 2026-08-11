#!/usr/bin/env python3
"""Tiny GTK4/WebKit kiosk for the Cerberus C3 status display.

The X server is owned by the companion systemd service.  This process only
creates one undecorated, screen-sized WebKit view and permits top-level
navigation to the configured loopback dashboard origin.
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlsplit


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def dashboard_origin(url: str) -> tuple[str, str, int]:
    """Return a validated loopback HTTP origin for *url*."""
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError("the kiosk URL must use HTTP on a loopback host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("the kiosk URL cannot contain credentials")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("the kiosk URL has an invalid port") from exc
    port = 80 if parsed_port is None else parsed_port
    if not 1 <= port <= 65535:
        raise ValueError("the kiosk URL port must be between 1 and 65535")
    return parsed.scheme, parsed.hostname, port


def navigation_is_allowed(candidate: str, dashboard_url: str) -> bool:
    """Allow navigation only within the dashboard's exact loopback origin."""
    if candidate == "about:blank":
        return True
    try:
        return dashboard_origin(candidate) == dashboard_origin(dashboard_url)
    except ValueError:
        return False


def parse_size(value: str) -> tuple[int, int]:
    """Parse a bounded WIDTHxHEIGHT display size."""
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("the kiosk size must be WIDTHxHEIGHT") from exc
    if not (64 <= width <= 16384 and 64 <= height <= 16384):
        raise ValueError("the kiosk dimensions must each be between 64 and 16384")
    return width, height


def load_runtime() -> tuple[object, object, object, object, object]:
    """Load the C3 runtime lazily so pure validation stays dependency-free."""
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("Gtk", "4.0")
    gi.require_version("WebKit", "6.0")
    from gi.repository import Gdk, Gio, GLib, Gtk, WebKit

    return Gdk, Gio, GLib, Gtk, WebKit


def runtime_version() -> str:
    _gdk, _gio, _glib, gtk, webkit = load_runtime()
    return (
        f"GTK {gtk.get_major_version()}.{gtk.get_minor_version()}."
        f"{gtk.get_micro_version()}, WebKitGTK {webkit.get_major_version()}."
        f"{webkit.get_minor_version()}.{webkit.get_micro_version()}"
    )


def run_kiosk(url: str, size: tuple[int, int], retry_seconds: int) -> int:
    Gdk, Gio, GLib, Gtk, WebKit = load_runtime()
    width, height = size

    class KioskWindow(Gtk.ApplicationWindow):
        def __init__(self, application: object) -> None:
            super().__init__(application=application)
            self._failed = False
            self._retry_source = 0
            self.set_title("DGX Spark cluster dashboard")
            self.set_decorated(False)
            self.set_resizable(False)
            self.set_default_size(width, height)

            self.webview = WebKit.WebView()
            self.webview.set_hexpand(True)
            self.webview.set_vexpand(True)
            self.webview.set_cursor_from_name("none")
            # Keep WebKit's underlay digital black. This prevents a bright or
            # merely dark frame during navigation, renderer restarts, and the
            # TFT maintenance sweep's first/last paint.
            self.webview.set_background_color(Gdk.RGBA(0, 0, 0, 1.0))
            settings = self.webview.get_settings()
            # Bare rootless Xorg has no compositor. On GB10, WebKit's
            # accelerated surface can remain white even though the page is
            # loaded and polling. Force the small local dashboard through the
            # software renderer so the X window owns real, capturable pixels.
            settings.set_hardware_acceleration_policy(
                WebKit.HardwareAccelerationPolicy.NEVER
            )
            settings.set_enable_developer_extras(False)
            if hasattr(settings, "set_enable_write_console_messages_to_stdout"):
                settings.set_enable_write_console_messages_to_stdout(True)

            self.webview.connect("load-changed", self._on_load_changed)
            self.webview.connect("load-failed", self._on_load_failed)
            self.webview.connect("decide-policy", self._on_decide_policy)
            self.webview.connect("context-menu", lambda *_args: True)
            self.webview.connect("permission-request", self._deny_permission)
            self.webview.connect(
                "web-process-terminated", self._on_web_process_terminated
            )
            self.connect("close-request", lambda *_args: True)
            self.set_child(self.webview)
            self.webview.load_uri(url)

        def _on_load_changed(self, _view: object, event: object) -> None:
            if event == WebKit.LoadEvent.STARTED:
                self._failed = False
            elif event == WebKit.LoadEvent.FINISHED and not self._failed:
                if self._retry_source:
                    GLib.source_remove(self._retry_source)
                    self._retry_source = 0
                print(f"C3 kiosk loaded {url}", file=sys.stderr, flush=True)

        def _on_load_failed(
            self, _view: object, _event: object, failing_uri: str, error: object
        ) -> bool:
            self._failed = True
            print(
                f"C3 kiosk load failed for {failing_uri}: {error}; retrying",
                file=sys.stderr,
                flush=True,
            )
            self._schedule_retry()
            return False

        def _on_web_process_terminated(
            self, _view: object, reason: object
        ) -> None:
            self._failed = True
            print(
                f"C3 kiosk web process terminated ({reason}); retrying",
                file=sys.stderr,
                flush=True,
            )
            self._schedule_retry()

        def _schedule_retry(self) -> None:
            if not self._retry_source:
                self._retry_source = GLib.timeout_add_seconds(
                    retry_seconds, self._retry
                )

        def _retry(self) -> bool:
            self._retry_source = 0
            self.webview.load_uri(url)
            return GLib.SOURCE_REMOVE

        def _on_decide_policy(
            self, _view: object, decision: object, decision_type: object
        ) -> bool:
            if decision_type not in (
                WebKit.PolicyDecisionType.NAVIGATION_ACTION,
                WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
            ):
                return False
            action = decision.get_navigation_action()
            candidate = action.get_request().get_uri()
            if navigation_is_allowed(candidate, url):
                return False
            print(
                f"C3 kiosk blocked navigation to {candidate}",
                file=sys.stderr,
                flush=True,
            )
            decision.ignore()
            return True

        @staticmethod
        def _deny_permission(_view: object, request: object) -> bool:
            request.deny()
            return True

    application = Gtk.Application(
        application_id="com.nvidia.DgxSpark.C3Dashboard",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    windows: list[object] = []

    def activate(app: object) -> None:
        window = KioskWindow(app)
        windows.append(window)
        window.present()
        window.fullscreen()

    application.connect("activate", activate)
    return int(application.run([sys.argv[0]]))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:9763/")
    parser.add_argument("--size", default="1424x280")
    parser.add_argument("--retry-seconds", type=int, default=5)
    parser.add_argument(
        "--check", action="store_true", help="validate runtime imports and exit"
    )
    args = parser.parse_args(argv)
    try:
        dashboard_origin(args.url)
        args.parsed_size = parse_size(args.size)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.retry_seconds <= 300:
        parser.error("--retry-seconds must be between 1 and 300")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.check:
        print(runtime_version())
        return 0
    return run_kiosk(args.url, args.parsed_size, args.retry_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
