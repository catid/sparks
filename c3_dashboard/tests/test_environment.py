import importlib.util
import pathlib
import tempfile
import unittest


DASHBOARD_DIR = pathlib.Path(__file__).parents[1]
VALIDATOR_PATH = DASHBOARD_DIR / "scripts" / "validate-environment.py"
SPEC = importlib.util.spec_from_file_location("c3_environment_validator", VALIDATOR_PATH)
validator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(validator)


class EnvironmentValidationTests(unittest.TestCase):
    def rendered_example(self) -> dict[str, str]:
        text = (DASHBOARD_DIR / "dashboard.env.example").read_text().replace(
            "@HOME@", "/home/dashboard"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "dashboard.env"
            path.write_text(text)
            return validator.parse_environment(path)

    def test_rendered_example_satisfies_complete_runtime_contract(self) -> None:
        values = self.rendered_example()
        validator.validate(values)
        self.assertEqual(values["C3_DASHBOARD_HISTORY_POINTS"], "60")
        self.assertEqual(values["C3_DASHBOARD_VOICE_STALE_SECONDS"], "6")

    def test_rejects_runtime_restart_loop_and_exposure_settings(self) -> None:
        cases = {
            "remote bind": ("C3_DASHBOARD_HOST", "0.0.0.0"),
            "remote opt-in": ("C3_DASHBOARD_ALLOW_REMOTE", "1"),
            "wrong interval": ("C3_DASHBOARD_INTERVAL", "2"),
            "hour-long history": ("C3_DASHBOARD_HISTORY_POINTS", "720"),
            "short history": ("C3_DASHBOARD_HISTORY_POINTS", "59"),
            "retry too high": ("C3_KIOSK_RETRY_SECONDS", "301"),
            "wait too high": ("C3_KIOSK_OUTPUT_WAIT_SECONDS", "301"),
            "bad output": ("C3_KIOSK_OUTPUT", "TV-0;bad"),
            "credential URL": (
                "C3_KIOSK_URL",
                "http://user:pass@127.0.0.1:9763/",
            ),
            "missing kiosk page": (
                "C3_KIOSK_URL",
                "http://127.0.0.1:9763/missing",
            ),
            "kiosk fragment": (
                "C3_KIOSK_URL",
                "http://127.0.0.1:9763/#not-the-root",
            ),
            "unexpanded key": ("C3_DASHBOARD_SSH_KEY", "@HOME@/.ssh/key"),
            "remote metrics": (
                "C3_DASHBOARD_VLLM_METRICS_URL",
                "http://example.com:8889/metrics",
            ),
            "voice status outside runtime": (
                "C3_DASHBOARD_VOICE_STATUS_PATH",
                "/home/dashboard/status.json",
            ),
            "voice status traversal": (
                "C3_DASHBOARD_VOICE_STATUS_PATH",
                "/run/voice/../../etc/shadow",
            ),
            "voice staleness too short": (
                "C3_DASHBOARD_VOICE_STALE_SECONDS",
                "2",
            ),
        }
        for label, (name, value) in cases.items():
            values = self.rendered_example()
            values[name] = value
            with self.subTest(label=label), self.assertRaises(ValueError):
                validator.validate(values)

        values = self.rendered_example()
        values["PATH"] = "/tmp/untrusted"
        with self.assertRaises(ValueError):
            validator.validate(values)

    def test_display_and_vt_are_owned_by_the_unit(self) -> None:
        for forbidden in ("C3_KIOSK_DISPLAY", "C3_KIOSK_VT"):
            values = self.rendered_example()
            values[forbidden] = ":9" if forbidden.endswith("DISPLAY") else "vt9"
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                validator.validate(values)

    def test_collector_is_restarted_after_clean_or_failed_exit(self) -> None:
        unit = (
            DASHBOARD_DIR
            / "systemd"
            / "dgx-spark-c3-dashboard.service.in"
        ).read_text()
        self.assertIn("Restart=always\n", unit)
        self.assertNotIn("Restart=on-failure", unit)

    def test_collector_readiness_and_private_ssh_master_lifecycle(self) -> None:
        collector = (
            DASHBOARD_DIR / "systemd" / "dgx-spark-c3-dashboard.service.in"
        ).read_text()
        kiosk = (
            DASHBOARD_DIR / "systemd" / "dgx-spark-c3-kiosk.service.in"
        ).read_text()
        self.assertIn("Type=notify\n", collector)
        self.assertIn("NotifyAccess=main\n", collector)
        self.assertIn("RuntimeDirectory=dgx-spark-c3-dashboard\n", collector)
        self.assertIn("RuntimeDirectoryMode=0700\n", collector)
        self.assertIn("KillMode=control-group\n", collector)
        self.assertIn(
            "Environment=C3_DASHBOARD_SSH_CONTROL_DIR=/run/dgx-spark-c3-dashboard\n",
            collector,
        )
        self.assertIn("Requires=dgx-spark-c3-dashboard.service\n", kiosk)
        self.assertNotIn("SuccessExitStatus=1", kiosk)

    def test_parser_rejects_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "duplicate.env"
            path.write_text("C3_DASHBOARD_PORT=9763\nC3_DASHBOARD_PORT=8888\n")
            with self.assertRaises(ValueError):
                validator.parse_environment(path)


if __name__ == "__main__":
    unittest.main()
