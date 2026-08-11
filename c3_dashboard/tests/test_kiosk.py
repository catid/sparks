#!/usr/bin/env python3

import contextlib
import io
import unittest

from c3_dashboard import kiosk


class DashboardOriginTests(unittest.TestCase):
    def test_accepts_loopback_http_origins(self) -> None:
        self.assertEqual(
            kiosk.dashboard_origin("http://127.0.0.1:9763/path"),
            ("http", "127.0.0.1", 9763),
        )
        self.assertEqual(
            kiosk.dashboard_origin("http://[::1]:9763/"),
            ("http", "::1", 9763),
        )
        self.assertEqual(
            kiosk.dashboard_origin("http://localhost/"),
            ("http", "localhost", 80),
        )

    def test_rejects_remote_tls_credentials_and_bad_ports(self) -> None:
        rejected = (
            "https://127.0.0.1:9763/",
            "http://cerebrus1:9763/",
            "http://user:password@127.0.0.1:9763/",
            "http://127.0.0.1:0/",
            "http://127.0.0.1:99999/",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                kiosk.dashboard_origin(value)

    def test_navigation_stays_on_exact_origin(self) -> None:
        dashboard = "http://127.0.0.1:9763/"
        self.assertTrue(kiosk.navigation_is_allowed("about:blank", dashboard))
        self.assertTrue(
            kiosk.navigation_is_allowed(
                "http://127.0.0.1:9763/details#gpu", dashboard
            )
        )
        self.assertFalse(
            kiosk.navigation_is_allowed("http://localhost:9763/", dashboard)
        )
        self.assertFalse(
            kiosk.navigation_is_allowed("http://127.0.0.1:8889/", dashboard)
        )
        self.assertFalse(kiosk.navigation_is_allowed("javascript:alert(1)", dashboard))


class SizeTests(unittest.TestCase):
    def test_native_size(self) -> None:
        self.assertEqual(kiosk.parse_size("1424x280"), (1424, 280))
        self.assertEqual(kiosk.parse_size("1424X280"), (1424, 280))

    def test_rejects_malformed_or_unbounded_sizes(self) -> None:
        for value in ("1424", "0x280", "1424x0", "1x1", "99999x280"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                kiosk.parse_size(value)

    def test_argument_contract(self) -> None:
        args = kiosk.parse_args(
            [
                "--url",
                "http://127.0.0.1:9763/",
                "--size",
                "1424x280",
                "--retry-seconds",
                "5",
            ]
        )
        self.assertEqual(args.parsed_size, (1424, 280))
        self.assertEqual(args.retry_seconds, 5)

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                kiosk.parse_args(["--retry-seconds", "0"])


if __name__ == "__main__":
    unittest.main()
