import importlib.util
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.request


SERVER = pathlib.Path(__file__).parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("c3_dashboard_server", SERVER)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(dashboard)


class FakeProbe:
    def __init__(self, down=()):
        self.calls = {}
        self.down = set(down)
        self.lock = threading.Lock()

    def __call__(self, host):
        if host in self.down:
            return {"error": "ssh timeout"}
        with self.lock:
            call = self.calls.get(host, 0)
            self.calls[host] = call + 1
        host_index = int(host[-1])
        return {
            "reported_hostname": host,
            "cpu_total": 1000 + call * 100,
            "cpu_idle": 600 + call * 40,
            "gpu_percent": host_index * 10,
            "cpu_temperature_c": 40 + host_index,
            "gpu_temperature_c": 50 + host_index,
            "soc_temperature_c": 45 + host_index,
            # GB10 exposes no LPDDR5X temperature sensor on these hosts.
            "ram_temperature_c": None,
            "memory_temperature_c": None,
            "memory_temperature_sensor_available": False,
            "ram_total_bytes": 1000,
            "ram_available_bytes": 1000 - host_index * 200,
        }


class FakeMetrics:
    def __init__(self, counters):
        self.counters = iter(counters)

    def __call__(self, _url, _timeout):
        value = next(self.counters)
        if isinstance(value, Exception):
            raise value
        return f"""
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{{model_name="ds4f"}} {value}
"""


class ServerTests(unittest.TestCase):
    @staticmethod
    def voice_payload(now=100.0):
        return {
            "schema": 1,
            "service": "cerberus-voice",
            "device": "Cerberus",
            "pid": 123,
            "sequence": 7,
            "updated_at": dashboard.utc_timestamp(now),
            "updated_at_epoch": now,
            "overall": {
                "state": "busy",
                "stage": "openclaw",
                "stage_started_at": dashboard.utc_timestamp(now - 3),
            },
            "wake_word": {
                "state": "triggered",
                "last_trigger_at": dashboard.utc_timestamp(now - 5),
                "armed_until": dashboard.utc_timestamp(now + 7),
            },
            "asr": {
                "state": "ok",
                "duration_seconds": 1.21,
                "last_success_at": dashboard.utc_timestamp(now - 4),
            },
            "openclaw": {
                "state": "thinking",
                "started_at": dashboard.utc_timestamp(now - 3),
            },
            "tts": {
                "state": "idle",
                "chunk_index": 0,
                "chunk_total": 0,
            },
            "last_error": {
                "stage": "tts_synthesis",
                "type": "TimeoutError",
                "at": dashboard.utc_timestamp(now - 20),
            },
            # These content-bearing fields must never enter the public payload.
            "transcript": "private microphone text",
            "response": "private model reply",
            "token": "private bearer token",
        }

    def test_voice_status_reader_whitelists_and_derives_live_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "status.json"
            path.write_text(json.dumps(self.voice_payload()))
            status = dashboard.VoiceStatusReader(str(path), 15).read(now=105)

        self.assertEqual(status["device"], "Cerberus")
        self.assertEqual(status["state"], "busy")
        self.assertEqual(status["stage"], "openclaw")
        self.assertEqual(status["stage_elapsed_seconds"], 8)
        self.assertEqual(status["watchword"]["state"], "triggered")
        self.assertEqual(status["watchword"]["armed_remaining_seconds"], 2)
        self.assertEqual(status["asr"]["duration_seconds"], 1.21)
        self.assertEqual(status["openclaw"]["elapsed_seconds"], 8)
        self.assertEqual(status["last_error"]["error_type"], "timeouterror")
        serialized = json.dumps(status)
        self.assertNotIn("private microphone", serialized)
        self.assertNotIn("private model", serialized)
        self.assertNotIn("bearer token", serialized)

    def test_voice_status_reader_marks_missing_malformed_stale_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            missing = dashboard.VoiceStatusReader(str(root / "missing.json"), 15)
            self.assertEqual(missing.read(now=100)["status_error"], "missing")

            malformed_path = root / "malformed.json"
            malformed_path.write_text("{")
            malformed = dashboard.VoiceStatusReader(str(malformed_path), 15)
            self.assertEqual(malformed.read(now=100)["status_error"], "malformed")

            stale_path = root / "stale.json"
            stale_path.write_text(json.dumps(self.voice_payload(now=50)))
            stale = dashboard.VoiceStatusReader(str(stale_path), 15).read(now=100)
            self.assertEqual(stale["state"], "stale")
            self.assertEqual(stale["status_error"], "stale")
            self.assertEqual(stale["age_seconds"], 50)
            self.assertEqual(stale["stage_elapsed_seconds"], 3)
            self.assertEqual(
                dashboard.VoiceStatusReader(str(stale_path), 15).read(now=200)[
                    "stage_elapsed_seconds"
                ],
                3,
            )

            link_path = root / "linked.json"
            os.symlink(stale_path, link_path)
            linked = dashboard.VoiceStatusReader(str(link_path), 15).read(now=100)
            self.assertEqual(linked["state"], "down")
            self.assertEqual(linked["status_error"], "invalid")

    def test_voice_status_reader_rejects_wrong_schema_and_oversize_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            schema_path = root / "schema.json"
            schema_path.write_text(json.dumps({"schema": 2, "service": "other"}))
            mismatch = dashboard.VoiceStatusReader(str(schema_path), 15).read(now=100)
            self.assertEqual(mismatch["status_error"], "schema_mismatch")

            schema_path.write_text(json.dumps({"schema": True, "service": "cerberus-voice"}))
            boolean_schema = dashboard.VoiceStatusReader(str(schema_path), 15).read(now=100)
            self.assertEqual(boolean_schema["status_error"], "schema_mismatch")

            future_path = root / "future.json"
            future_path.write_text(json.dumps(self.voice_payload(now=1_000)))
            future = dashboard.VoiceStatusReader(str(future_path), 15).read(now=100)
            self.assertEqual(future["status_error"], "invalid")

            large_path = root / "large.json"
            large_path.write_bytes(b"x" * (dashboard.MAX_VOICE_STATUS_BYTES + 1))
            oversized = dashboard.VoiceStatusReader(str(large_path), 15).read(now=100)
            self.assertEqual(oversized["status_error"], "invalid")

    def test_fast_voice_endpoint_is_distinct_from_cluster_snapshot(self):
        class FakeCollector:
            def __init__(self):
                self.voice_calls = 0

            def get_snapshot(self):
                return {"generated_at": "cluster-snapshot", "voice_agent": {"sequence": 1}}

            def get_voice_status(self):
                self.voice_calls += 1
                return {"service": "cerberus-voice", "sequence": 2}

        collector = FakeCollector()
        server = dashboard.ThreadingHTTPServer(
            ("127.0.0.1", 0), dashboard.DashboardHandler
        )
        server.collector = collector
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urllib.request.urlopen(f"{base}/api/voice-status", timeout=2) as response:
                voice = json.load(response)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
            with urllib.request.urlopen(f"{base}/api/status", timeout=2) as response:
                cluster = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(voice["sequence"], 2)
        self.assertEqual(cluster["voice_agent"]["sequence"], 1)
        self.assertEqual(collector.voice_calls, 1)

    def test_probe_parser_and_cpu_delta(self):
        parsed = dashboard.parse_probe(
            """HOSTNAME=cerebrus3
CPU=1000,600
MEMORY=1000,250
GPU=25,46
GPU=75,48
THERMAL=\\_TZ_.TSOC,51200
THERMAL=\\_TZ_.TS0E,43100
THERMAL=\\_TZ_.TS0P,54800
THERMAL=\\_TZ_.TS1E,42700
THERMAL=\\_TZ_.TS1P,44000
THERMAL=\\_TZ_.TGPU,99000
THERMAL=MEMORY,41850
"""
        )
        self.assertEqual(parsed["reported_hostname"], "cerebrus3")
        self.assertEqual(parsed["gpu_percent"], 50)
        # nvidia-smi wins over the firmware TGPU fallback.
        self.assertEqual(parsed["gpu_temperature_c"], 48)
        self.assertEqual(parsed["cpu_temperature_c"], 54.8)
        self.assertEqual(parsed["soc_temperature_c"], 51.2)
        self.assertEqual(parsed["ram_temperature_c"], 41.9)
        self.assertEqual(parsed["memory_temperature_c"], 41.9)
        self.assertTrue(parsed["memory_temperature_sensor_available"])
        self.assertEqual(parsed["ram_total_bytes"], 1000 * 1024)
        self.assertEqual(
            dashboard.cpu_percent(1100, 640, (1000, 600)),
            60,
        )

    def test_probe_thermal_parser_uses_named_sensors_and_honest_missing_ram(self):
        parsed = dashboard.parse_probe(
            """GPU=10,[N/A]
THERMAL=\\_TZ_.TGPU,46700
THERMAL=\\_TZ_.TS0E,43000
THERMAL=\\_TZ_.TS0P,44000
THERMAL=\\_TZ_.ACPI0000,99000
THERMAL=MEMORY,999999
"""
        )
        self.assertEqual(parsed["gpu_temperature_c"], 46.7)
        self.assertEqual(parsed["cpu_temperature_c"], 44)
        self.assertIsNone(parsed["soc_temperature_c"])
        self.assertIsNone(parsed["ram_temperature_c"])
        self.assertIsNone(parsed["memory_temperature_c"])
        self.assertFalse(parsed["memory_temperature_sensor_available"])

    def test_cpu_delta_rejects_first_sample_and_counter_reset(self):
        self.assertIsNone(dashboard.cpu_percent(100, 40, None))
        self.assertIsNone(dashboard.cpu_percent(90, 30, (100, 40)))
        self.assertIsNone(dashboard.cpu_percent(100, 30, (100, 30)))

    def test_prometheus_prefers_one_total_and_does_not_double_count(self):
        total, metric = dashboard.generation_counter(
            """# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{engine="0"} 100
vllm:generation_tokens_total{engine="1"} 20
# TYPE vllm:request_generation_tokens histogram
vllm:request_generation_tokens_sum 999
"""
        )
        self.assertEqual(total, 120)
        self.assertEqual(metric, "vllm:generation_tokens_total")

    def test_prometheus_rejects_wrong_type_and_uses_histogram_fallback(self):
        total, metric = dashboard.generation_counter(
            """# TYPE vllm:generation_tokens_total gauge
vllm:generation_tokens_total 99999
# TYPE vllm:request_generation_tokens histogram
vllm:request_generation_tokens_sum 45
"""
        )
        self.assertEqual(total, 45)
        self.assertEqual(metric, "vllm:request_generation_tokens_sum")

    def test_throughput_counter_is_never_shown_as_a_rate(self):
        tracker = dashboard.ThroughputTracker("http://c1:8889/metrics", 10)
        first = tracker.success(48_000, "vllm:generation_tokens_total", 100)
        active = tracker.success(48_100, "vllm:generation_tokens_total", 105)
        idle = tracker.success(48_100, "vllm:generation_tokens_total", 110)

        self.assertEqual(first["state"], "warming")
        self.assertIsNone(first["tokens_per_second"])
        self.assertEqual(active["state"], "active")
        self.assertEqual(active["tokens_per_second"], 20)
        self.assertEqual(idle["state"], "idle")
        self.assertEqual(idle["tokens_per_second"], 0)
        self.assertNotIn("counter_total", active)

    def test_throughput_reset_stale_and_down_are_explicit(self):
        tracker = dashboard.ThroughputTracker("http://c1:8889/metrics", 10)
        tracker.success(100, "vllm:generation_tokens_total", 100)
        reset = tracker.success(2, "vllm:generation_tokens_total", 105)
        stale = tracker.failure("timed out", 111)
        down = tracker.failure("timed out", 116)

        self.assertEqual(reset["state"], "warming")
        self.assertIsNone(reset["tokens_per_second"])
        self.assertIn("reset", reset["reason"])
        self.assertEqual(stale["state"], "stale")
        self.assertIsNone(stale["tokens_per_second"])
        self.assertEqual(down["state"], "down")

    def test_collector_reports_hosts_cluster_average_history_and_rate(self):
        with tempfile.TemporaryDirectory() as directory:
            voice_path = pathlib.Path(directory) / "status.json"
            voice_path.write_text(json.dumps(self.voice_payload(now=105)))
            collector = dashboard.Collector(
                interval=5,
                host_prober=FakeProbe(),
                metrics_fetcher=FakeMetrics([100, 125]),
                history_points=2,
                voice_status_path=str(voice_path),
            )
            collector.collect(now=100)
            first = collector.get_snapshot()
            collector.collect(now=105)
            snapshot = collector.get_snapshot()

        self.assertIsNone(first["cluster"]["cpu_percent"])
        self.assertEqual(snapshot["cluster"]["state"], "up")
        self.assertEqual(snapshot["cluster"]["cpu_percent"], 60)
        self.assertEqual(snapshot["cluster"]["gpu_percent"], 20)
        self.assertEqual(snapshot["cluster"]["ram_percent"], 40)
        self.assertEqual(snapshot["cluster"]["cpu_temperature_c"], 42)
        self.assertEqual(snapshot["cluster"]["gpu_temperature_c"], 52)
        self.assertEqual(snapshot["cluster"]["soc_temperature_c"], 47)
        self.assertIsNone(snapshot["cluster"]["ram_temperature_c"])
        self.assertIsNone(snapshot["cluster"]["memory_temperature_c"])
        self.assertEqual(snapshot["cluster"]["sampled_hosts"]["cpu_temperature"], 3)
        self.assertEqual(snapshot["cluster"]["sampled_hosts"]["gpu_temperature"], 3)
        self.assertEqual(snapshot["cluster"]["sampled_hosts"]["ram_temperature"], 0)
        self.assertEqual(snapshot["hosts"]["cerebrus2"]["ram_percent"], 40)
        self.assertEqual(snapshot["hosts"]["cerebrus2"]["cpu_temperature_c"], 42)
        self.assertEqual(snapshot["hosts"]["cerebrus2"]["gpu_temperature_c"], 52)
        self.assertFalse(
            snapshot["hosts"]["cerebrus2"]["memory_temperature_sensor_available"]
        )
        self.assertEqual(snapshot["throughput"]["tokens_per_second"], 5)
        self.assertEqual(snapshot["throughput"]["state"], "active")
        self.assertEqual(snapshot["throughput"]["source"], "vllm")
        self.assertEqual(snapshot["voice_agent"]["stage"], "openclaw")
        self.assertNotIn("cerebrus1:8889", str(snapshot))
        self.assertEqual(len(snapshot["history"]), 2)
        self.assertIn("cluster", snapshot["history"][-1])
        self.assertIn("hosts", snapshot["history"][-1])
        history_host = snapshot["history"][-1]["hosts"]["cerebrus2"]
        self.assertEqual(history_host["cpu_temperature_c"], 42)
        self.assertEqual(history_host["gpu_temperature_c"], 52)
        self.assertIsNone(history_host["ram_temperature_c"])

    def test_failed_host_is_down_and_cluster_is_degraded(self):
        collector = dashboard.Collector(
            interval=5,
            host_prober=FakeProbe(down=("cerebrus2",)),
            metrics_fetcher=FakeMetrics([100]),
        )
        collector.collect(now=100)
        snapshot = collector.get_snapshot()

        self.assertEqual(snapshot["hosts"]["cerebrus2"]["state"], "down")
        self.assertEqual(snapshot["hosts"]["cerebrus2"]["error"], "ssh timeout")
        self.assertEqual(snapshot["cluster"]["state"], "degraded")
        self.assertEqual(snapshot["cluster"]["available_hosts"], 2)
        self.assertEqual(snapshot["cluster"]["sampled_hosts"]["gpu"], 2)

    def test_scrape_failure_does_not_reuse_last_rate(self):
        collector = dashboard.Collector(
            interval=5,
            host_prober=FakeProbe(),
            metrics_fetcher=FakeMetrics(
                [100, 125, OSError("connection refused"), 225, 250]
            ),
        )
        collector.collect(now=100)
        collector.collect(now=105)
        self.assertEqual(
            collector.get_snapshot()["throughput"]["tokens_per_second"], 5
        )
        collector.collect(now=110)
        throughput = collector.get_snapshot()["throughput"]
        self.assertEqual(throughput["state"], "stale")
        self.assertIsNone(throughput["tokens_per_second"])

        collector.collect(now=115)
        recovered = collector.get_snapshot()["throughput"]
        self.assertEqual(recovered["state"], "warming")
        self.assertIsNone(recovered["tokens_per_second"])

        collector.collect(now=120)
        live = collector.get_snapshot()["throughput"]
        self.assertEqual(live["state"], "active")
        self.assertEqual(live["tokens_per_second"], 5)

    def test_history_is_bounded(self):
        collector = dashboard.Collector(
            interval=5,
            host_prober=FakeProbe(),
            metrics_fetcher=FakeMetrics([100, 100, 100]),
            history_points=2,
        )
        collector.collect(now=100)
        collector.collect(now=105)
        collector.collect(now=110)
        history = collector.get_snapshot()["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["timestamp"], dashboard.utc_timestamp(105))

    def test_host_validation_blocks_ssh_option_injection_and_alias_duplicates(self):
        with self.assertRaises(ValueError):
            dashboard.validate_nodes(("-oProxyCommand=bad",))
        with self.assertRaises(ValueError):
            dashboard.validate_nodes(("spark1", "cerebrus1"))

    def test_local_alias_uses_shell_while_remote_uses_strict_ssh(self):
        prober = dashboard.HostProber(
            "/cluster-key", "/known-hosts", local_hostname="cerebrus3"
        )
        self.assertEqual(prober.command("spark3"), ["sh", "-s"])
        remote = prober.command("cerebrus1")
        self.assertEqual(remote[0], "ssh")
        self.assertIn("StrictHostKeyChecking=yes", remote)
        self.assertIn("UserKnownHostsFile=/known-hosts", remote)


if __name__ == "__main__":
    unittest.main()
