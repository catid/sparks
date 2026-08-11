import importlib.util
import pathlib
import threading
import unittest


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
    def test_probe_parser_and_cpu_delta(self):
        parsed = dashboard.parse_probe(
            """HOSTNAME=cerebrus3
CPU=1000,600
MEMORY=1000,250
GPU=25
GPU=75
"""
        )
        self.assertEqual(parsed["reported_hostname"], "cerebrus3")
        self.assertEqual(parsed["gpu_percent"], 50)
        self.assertEqual(parsed["ram_total_bytes"], 1000 * 1024)
        self.assertEqual(
            dashboard.cpu_percent(1100, 640, (1000, 600)),
            60,
        )

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
        collector = dashboard.Collector(
            interval=5,
            host_prober=FakeProbe(),
            metrics_fetcher=FakeMetrics([100, 125]),
            history_points=2,
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
        self.assertEqual(snapshot["hosts"]["cerebrus2"]["ram_percent"], 40)
        self.assertEqual(snapshot["throughput"]["tokens_per_second"], 5)
        self.assertEqual(snapshot["throughput"]["state"], "active")
        self.assertEqual(snapshot["throughput"]["source"], "vllm")
        self.assertNotIn("cerebrus1:8889", str(snapshot))
        self.assertEqual(len(snapshot["history"]), 2)
        self.assertIn("cluster", snapshot["history"][-1])
        self.assertIn("hosts", snapshot["history"][-1])

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
