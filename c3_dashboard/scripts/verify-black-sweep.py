#!/usr/bin/env python3
"""Verify the C3 TFT's exact-black maintenance band across every panel row.

Run as the kiosk user with DISPLAY and XAUTHORITY set. The verifier cheaply
samples four rows at the top of the real X root until the band arrives, then
captures the complete 1424x280 framebuffer through the one 3.2-second sweep.
It succeeds only when every row is observed as exact RGB black for at least
three samples spanning 100 ms. No dashboard debug endpoint is involved.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class FrameAnalysis:
    width: int
    height: int
    total_pixels: int
    nonblack_pixels: int
    brightest_channel: int
    black_rows: tuple[bool, ...]

    @property
    def all_rows_black(self) -> bool:
        return bool(self.black_rows) and all(self.black_rows)


class SweepCoverage:
    """Track a consecutive exact-black dwell independently for every row."""

    def __init__(
        self,
        height: int,
        *,
        minimum_samples: int = 3,
        minimum_seconds: float = 0.1,
    ) -> None:
        if height <= 0:
            raise ValueError("height must be positive")
        if minimum_samples < 1:
            raise ValueError("minimum_samples must be positive")
        if not math.isfinite(minimum_seconds) or minimum_seconds < 0:
            raise ValueError("minimum_seconds must be finite and non-negative")
        self.height = height
        self.minimum_samples = minimum_samples
        self.minimum_seconds = minimum_seconds
        self.consecutive = [0] * height
        self.first_black_at: list[float | None] = [None] * height
        self.longest_black_seconds = [0.0] * height
        self.satisfied = [False] * height

    @property
    def covered_rows(self) -> int:
        return sum(self.satisfied)

    @property
    def complete(self) -> bool:
        return self.covered_rows == self.height

    @property
    def minimum_observed_black_seconds(self) -> float:
        return min(self.longest_black_seconds, default=0.0)

    def observe(self, frame: FrameAnalysis, observed_at: float) -> bool:
        if frame.height != self.height or len(frame.black_rows) != self.height:
            raise ValueError("frame height does not match sweep tracker")
        if not math.isfinite(observed_at):
            raise ValueError("observation time must be finite")
        for row, is_black in enumerate(frame.black_rows):
            if not is_black:
                self.consecutive[row] = 0
                self.first_black_at[row] = None
                continue
            if self.first_black_at[row] is None:
                self.first_black_at[row] = observed_at
                self.consecutive[row] = 0
            self.consecutive[row] += 1
            elapsed = max(0.0, observed_at - self.first_black_at[row])
            self.longest_black_seconds[row] = max(
                self.longest_black_seconds[row], elapsed
            )
            if (
                self.consecutive[row] >= self.minimum_samples
                and elapsed >= self.minimum_seconds
            ):
                self.satisfied[row] = True
        return self.complete


@dataclass(frozen=True)
class VerificationResult:
    complete: bool
    detected: bool
    probe_samples: int
    sweep_samples: int
    covered_rows: int
    total_rows: int
    minimum_black_seconds: float


def wait_for_black_sweep(
    probe: Callable[[], FrameAnalysis],
    capture: Callable[[], FrameAnalysis],
    *,
    wait_seconds: float,
    sample_seconds: float,
    sweep_seconds: float = 5.0,
    minimum_samples: int = 3,
    minimum_seconds: float = 0.1,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> VerificationResult:
    """Detect the band's top edge, then measure one bounded complete pass."""
    for value, label in (
        (wait_seconds, "wait_seconds"),
        (sample_seconds, "sample_seconds"),
        (sweep_seconds, "sweep_seconds"),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be finite and positive")
    deadline = monotonic() + wait_seconds
    probe_samples = 0
    while True:
        probe_frame = probe()
        probe_samples += 1
        if probe_frame.all_rows_black:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            return VerificationResult(
                False, False, probe_samples, 0, 0, capture().height, 0.0
            )
        sleep(min(sample_seconds, remaining))

    first = capture()
    # The normal dashboard must still be visible below a top-connected band.
    # A completely black X root means WebKit may be absent or crashed; accepting
    # that would verify the fail-safe underlay instead of the moving sweep.
    if first.all_rows_black or not first.black_rows[0]:
        return VerificationResult(
            False,
            True,
            probe_samples,
            1,
            0,
            first.height,
            0.0,
        )
    coverage = SweepCoverage(
        first.height,
        minimum_samples=minimum_samples,
        minimum_seconds=minimum_seconds,
    )
    sweep_samples = 1
    coverage.observe(first, monotonic())
    # The idle wait and the measurement window are independent. Detection on
    # the final allowed probe still receives a complete bounded sweep window.
    sweep_deadline = monotonic() + sweep_seconds
    while not coverage.complete:
        remaining = sweep_deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(sample_seconds, remaining))
        sweep_samples += 1
        coverage.observe(capture(), monotonic())
    return VerificationResult(
        coverage.complete,
        True,
        probe_samples,
        sweep_samples,
        coverage.covered_rows,
        coverage.height,
        coverage.minimum_observed_black_seconds,
    )


def parse_size(value: str) -> tuple[int, int]:
    try:
        width_text, height_text = value.lower().split("x", 1)
        width, height = int(width_text), int(height_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("expected WIDTHxHEIGHT") from exc
    if not (64 <= width <= 16384 and 64 <= height <= 16384):
        raise ValueError("dimensions must each be between 64 and 16384")
    return width, height


def analyze_rgb(
    pixels: bytes,
    *,
    width: int,
    height: int,
    rowstride: int,
    channels: int,
) -> FrameAnalysis:
    """Find exact-black rows in a packed, possibly padded RGB(A) buffer."""
    if width <= 0 or height <= 0:
        raise ValueError("frame dimensions must be positive")
    if channels not in (3, 4):
        raise ValueError("only RGB and RGBA frames are supported")
    if rowstride < width * channels:
        raise ValueError("rowstride is shorter than one pixel row")
    if len(pixels) < rowstride * height:
        raise ValueError("pixel buffer is shorter than the described frame")

    nonblack = 0
    brightest = 0
    black_rows: list[bool] = []
    view = memoryview(pixels)
    for y in range(height):
        row = view[y * rowstride : y * rowstride + width * channels]
        row_black = True
        for offset in range(0, len(row), channels):
            red, green, blue = row[offset : offset + 3]
            pixel_max = max(red, green, blue)
            if pixel_max:
                row_black = False
                nonblack += 1
                brightest = max(brightest, pixel_max)
        black_rows.append(row_black)
    return FrameAnalysis(
        width=width,
        height=height,
        total_pixels=width * height,
        nonblack_pixels=nonblack,
        brightest_channel=brightest,
        black_rows=tuple(black_rows),
    )


def capture_root_region(
    expected_size: tuple[int, int], y: int, height: int
) -> FrameAnalysis:
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk
    except (ImportError, ValueError) as exc:
        raise RuntimeError(f"Python GDK 3 is unavailable: {exc}") from exc

    display = Gdk.Display.get_default()
    root = Gdk.get_default_root_window()
    if display is None or root is None:
        raise RuntimeError("cannot connect to the configured X display")
    display.sync()
    width, root_height = root.get_width(), root.get_height()
    if (width, root_height) != expected_size:
        raise RuntimeError(
            f"X root is {width}x{root_height}, expected "
            f"{expected_size[0]}x{expected_size[1]}"
        )
    if y < 0 or height <= 0 or y + height > root_height:
        raise RuntimeError("capture region is outside the X root")
    pixbuf = Gdk.pixbuf_get_from_window(root, 0, y, width, height)
    if pixbuf is None:
        raise RuntimeError("GDK could not capture the X root window")
    return analyze_rgb(
        bytes(pixbuf.get_pixels()),
        width=pixbuf.get_width(),
        height=pixbuf.get_height(),
        rowstride=pixbuf.get_rowstride(),
        channels=pixbuf.get_n_channels(),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    parser.add_argument(
        "--xauthority",
        default=os.environ.get(
            "XAUTHORITY", "/run/dgx-spark-c3-kiosk/.Xauthority"
        ),
    )
    parser.add_argument("--size", default="1424x280")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=330,
        help="wait this long for the next sweep to begin (default: 330)",
    )
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=0.05,
        help="probe/capture interval in seconds (default: 0.05)",
    )
    parser.add_argument(
        "--sweep-seconds",
        type=float,
        default=5.0,
        help="maximum time allowed for a detected sweep (default: 5)",
    )
    args = parser.parse_args(argv)
    try:
        args.parsed_size = parse_size(args.size)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.wait_seconds <= 3600:
        parser.error("--wait-seconds must be between 1 and 3600")
    if not 0.02 <= args.sample_seconds <= 1:
        parser.error("--sample-seconds must be between 0.02 and 1")
    if not 1 <= args.sweep_seconds <= 30:
        parser.error("--sweep-seconds must be between 1 and 30")
    if not args.display or not args.xauthority:
        parser.error("--display and --xauthority cannot be empty")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ["DISPLAY"] = args.display
    os.environ["XAUTHORITY"] = args.xauthority
    size = args.parsed_size
    try:
        result = wait_for_black_sweep(
            lambda: capture_root_region(size, 0, 4),
            lambda: capture_root_region(size, 0, size[1]),
            wait_seconds=args.wait_seconds,
            sample_seconds=args.sample_seconds,
            sweep_seconds=args.sweep_seconds,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"black-sweep verification failed: {exc}", file=sys.stderr)
        return 2

    payload = {
        "state": "complete_sweep" if result.complete else "incomplete_sweep",
        "detected": result.detected,
        "probe_samples": result.probe_samples,
        "sweep_samples": result.sweep_samples,
        "covered_rows": result.covered_rows,
        "total_rows": result.total_rows,
        "minimum_black_seconds": round(result.minimum_black_seconds, 3),
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if result.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
