#!/usr/bin/env python3

import importlib.util
import pathlib
import sys
import unittest


SCRIPT = pathlib.Path(__file__).parents[1] / "scripts" / "verify-black-sweep.py"
SPEC = importlib.util.spec_from_file_location("verify_black_sweep", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
verify_black_sweep = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_black_sweep
SPEC.loader.exec_module(verify_black_sweep)


def frame(rows: tuple[bool, ...]) -> verify_black_sweep.FrameAnalysis:
    return verify_black_sweep.FrameAnalysis(
        width=1,
        height=len(rows),
        total_pixels=len(rows),
        nonblack_pixels=sum(not row for row in rows),
        brightest_channel=1 if not all(rows) else 0,
        black_rows=rows,
    )


class SizeTests(unittest.TestCase):
    def test_parses_native_panel_size(self) -> None:
        self.assertEqual(verify_black_sweep.parse_size("1424x280"), (1424, 280))

    def test_rejects_bad_or_unbounded_sizes(self) -> None:
        for value in ("1424", "0x280", "63x280", "99999x280"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify_black_sweep.parse_size(value)


class PixelAnalysisTests(unittest.TestCase):
    def test_exact_rgb_black_rows_with_padding(self) -> None:
        result = verify_black_sweep.analyze_rgb(
            bytes([0, 0, 0, 0, 0, 0, 99, 99, 1, 0, 0, 0, 0, 0, 99, 99]),
            width=2,
            height=2,
            rowstride=8,
            channels=3,
        )
        self.assertEqual(result.black_rows, (True, False))
        self.assertEqual(result.nonblack_pixels, 1)
        self.assertEqual(result.brightest_channel, 1)

    def test_rgba_alpha_is_ignored(self) -> None:
        result = verify_black_sweep.analyze_rgb(
            bytes([0, 0, 0, 255, 0, 0, 0, 17]),
            width=2,
            height=1,
            rowstride=8,
            channels=4,
        )
        self.assertTrue(result.all_rows_black)

    def test_rejects_inconsistent_buffers(self) -> None:
        with self.assertRaises(ValueError):
            verify_black_sweep.analyze_rgb(
                b"\0" * 3, width=2, height=1, rowstride=3, channels=3
            )


class CoverageTests(unittest.TestCase):
    def test_each_row_needs_three_samples_and_point_one_second(self) -> None:
        coverage = verify_black_sweep.SweepCoverage(2)
        self.assertFalse(coverage.observe(frame((True, False)), 0.0))
        self.assertFalse(coverage.observe(frame((True, True)), 0.05))
        self.assertFalse(coverage.observe(frame((True, True)), 0.10))
        self.assertEqual(coverage.covered_rows, 1)
        self.assertFalse(coverage.observe(frame((False, True)), 0.15))
        self.assertTrue(coverage.observe(frame((False, True)), 0.20))
        self.assertEqual(coverage.covered_rows, 2)

    def test_nonblack_sample_resets_only_that_rows_current_dwell(self) -> None:
        coverage = verify_black_sweep.SweepCoverage(1)
        coverage.observe(frame((True,)), 0.0)
        coverage.observe(frame((False,)), 0.08)
        coverage.observe(frame((True,)), 0.10)
        coverage.observe(frame((True,)), 0.15)
        self.assertFalse(coverage.observe(frame((True,)), 0.19))
        self.assertTrue(coverage.observe(frame((True,)), 0.20))

    def test_wait_detects_top_then_tracks_a_complete_downward_sweep(self) -> None:
        clock = [0.0]
        probes = iter([frame((False,)), frame((True,))])
        captures = iter(
            [
                frame((True, False)),
                frame((True, True)),
                frame((True, True)),
                frame((False, True)),
                frame((False, True)),
            ]
        )

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        result = verify_black_sweep.wait_for_black_sweep(
            lambda: next(probes),
            lambda: next(captures),
            wait_seconds=2,
            sample_seconds=0.05,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.detected)
        self.assertEqual(result.covered_rows, 2)

    def test_late_detection_gets_a_fresh_measurement_window(self) -> None:
        clock = [0.0]
        probes = iter([frame((False,)), frame((False,)), frame((True,))])
        captures = iter(
            [
                frame((True, False)),
                frame((True, True)),
                frame((True, True)),
                frame((False, True)),
                frame((False, True)),
            ]
        )

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        result = verify_black_sweep.wait_for_black_sweep(
            lambda: next(probes),
            lambda: next(captures),
            wait_seconds=0.11,
            sample_seconds=0.05,
            sweep_seconds=0.25,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.detected)
        self.assertGreater(clock[0], 0.11)

    def test_static_black_root_is_not_mistaken_for_a_sweep(self) -> None:
        result = verify_black_sweep.wait_for_black_sweep(
            lambda: frame((True,)),
            lambda: frame((True, True)),
            wait_seconds=1,
            sample_seconds=0.05,
        )
        self.assertFalse(result.complete)
        self.assertTrue(result.detected)
        self.assertEqual(result.covered_rows, 0)
        self.assertEqual(result.sweep_samples, 1)

    def test_wait_times_out_without_detection(self) -> None:
        clock = [0.0]

        def monotonic() -> float:
            return clock[0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        result = verify_black_sweep.wait_for_black_sweep(
            lambda: frame((False,)),
            lambda: frame((False, False)),
            wait_seconds=0.11,
            sample_seconds=0.05,
            monotonic=monotonic,
            sleep=sleep,
        )
        self.assertFalse(result.complete)
        self.assertFalse(result.detected)
        self.assertEqual(clock[0], 0.11)


if __name__ == "__main__":
    unittest.main()
