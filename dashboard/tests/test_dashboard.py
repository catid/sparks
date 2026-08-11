import importlib.util
import pathlib
import tempfile
import time
import unittest
from unittest import mock


SERVER = pathlib.Path(__file__).parents[1] / "server.py"
SPEC = importlib.util.spec_from_file_location("dashboard_server", SERVER)
dashboard = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(dashboard)


def make_collector(**overrides):
    values = {
        "spark2_host": "cerberus2",
        "ssh_key": "/cluster-key",
        "node_urls": {
            "cerberus1": "http://rank0:8000",
            "cerberus2": "http://rank1:8000",
        },
        "node_roles": {"cerberus1": "aggregate", "cerberus2": "worker"},
        "inference_mode": "direct",
        "router_url": "http://router:8080",
        "router_metrics_url": "http://router:29000",
        "interfaces": (),
        "interval": 2,
    }
    values.update(overrides)
    return dashboard.Collector(**values)


class DashboardTests(unittest.TestCase):
    def test_legacy_node_maps_are_accepted_but_normalized_to_cerberus(self):
        collector = make_collector(
            node_urls={
                "spark1": "http://legacy-rank0:8000",
                "spark2": "http://legacy-rank1:8000",
                "cerberus1": "http://canonical-rank0:8000",
            },
            node_roles={"spark1": "aggregate", "spark2": "worker"},
        )

        self.assertEqual(
            collector.node_urls["cerberus1"], "http://canonical-rank0:8000"
        )
        self.assertEqual(
            collector.node_urls["cerberus2"], "http://legacy-rank1:8000"
        )
        self.assertEqual(
            collector.node_roles,
            {"cerberus1": "aggregate", "cerberus2": "worker"},
        )
        self.assertNotIn("spark1", collector.node_urls)
        self.assertNotIn("spark2", collector.node_roles)

    def test_initial_cluster_contract_is_stable_before_first_sample(self):
        status = make_collector().get_snapshot()["cluster"]
        self.assertEqual(status["state"], "down")
        self.assertFalse(status["endpoint_healthy"])
        self.assertEqual(status["affected_nodes"], [])
        self.assertEqual(status["outage_elapsed_seconds"], 0.0)
        self.assertIn("endpoint", status)

    def test_thermal_stats_uses_named_gb10_zones_and_hwmon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            thermal_root = root / "thermal"
            hwmon_root = root / "hwmon"

            zones = {
                "TSOC": 51_200,
                "TS0E": 43_100,
                "TS0P": 54_800,
                "TS1E": 42_700,
                "TS1P": 44_000,
                "TGPU": 46_700,
                "TUNC": 45_500,
            }
            for index, (name, value) in enumerate(zones.items()):
                zone = thermal_root / f"thermal_zone{index}"
                (zone / "device").mkdir(parents=True)
                (zone / "device" / "path").write_text(f"\\\\_TZ_.{name}\n")
                (zone / "temp").write_text(f"{value}\n")

            for index, (driver, label, value) in enumerate(
                (
                    ("nvme", "Composite", 41_850),
                    ("mlx5", "asic", 48_000),
                    ("mlx5", "asic", 49_000),
                )
            ):
                device = hwmon_root / f"hwmon{index}"
                device.mkdir(parents=True)
                (device / "name").write_text(f"{driver}\n")
                (device / "temp1_label").write_text(f"{label}\n")
                (device / "temp1_input").write_text(f"{value}\n")

            thermals = dashboard.thermal_stats(thermal_root, hwmon_root)

        self.assertEqual(thermals["cpu_cluster_max_c"], 54.8)
        self.assertEqual(thermals["soc_c"], 51.2)
        self.assertEqual(thermals["firmware_gpu_c"], 46.7)
        self.assertEqual(thermals["nvme_composite_c"], 41.85)
        self.assertEqual(thermals["connectx_asic_max_c"], 49.0)
        self.assertIsNone(thermals["memory_c"])
        self.assertFalse(thermals["memory_sensor_available"])

    def test_thermal_summary_ignores_invalid_and_anonymous_values(self):
        thermals = dashboard.summarize_thermals(
            {
                "ACPI0000": [99.0],
                "TS0E": [43.0],
                "TS0P": [float("nan")],
            }
        )
        self.assertEqual(thermals["cpu_cluster_max_c"], 43.0)
        self.assertIsNone(thermals["soc_c"])
        self.assertIsNone(thermals["memory_c"])

    def test_prometheus_parser_and_metric_aliases(self):
        metrics = dashboard.parse_prometheus(
            """
# HELP vLLM
vllm:generation_tokens_total{model_name="laguna"} 120
vllm:generation_tokens_total{model_name="other"} 30
vllm:num_requests_running 2
garbage
"""
        )
        self.assertEqual(
            dashboard.metric_sum(metrics, "vllm:generation_tokens_total"), 150
        )
        self.assertEqual(dashboard.metric_avg(metrics, "vllm:num_requests_running"), 2)

    def test_counter_rates_are_reset_safe(self):
        self.assertEqual(dashboard.counter_rate(130, 100, 2), 15)
        self.assertIsNone(dashboard.counter_rate(90, 100, 2))
        self.assertIsNone(dashboard.counter_rate(None, 100, 2))

    def test_remote_probe_parser_handles_unified_memory(self):
        parsed = dashboard.parse_remote_probe(
            """HOSTNAME=cerberus2
GPU=NVIDIA GB10, 45, 88.1, 2400, 2400, 97, 41, [N/A], [N/A]
MEM_MemTotal:=1000
MEM_MemAvailable:=250
MEM_SwapTotal:=100
MEM_SwapFree:=90
VLLM_RSS=500
THERMAL=\\_TZ_.TSOC,51200
THERMAL=\\_TZ_.TS0E,43100
THERMAL=\\_TZ_.TS0P,54800
THERMAL=\\_TZ_.TS1E,42700
THERMAL=\\_TZ_.TS1P,44000
THERMAL=NVME,41850
THERMAL=MLX5,48000
NET=enp1s0f0np0,100,200,0,0,up,9000,rdma,rocep1s0f0,10,20
"""
        )
        self.assertEqual(parsed["memory"]["used_bytes"], 750)
        self.assertIsNone(parsed["gpu"]["framebuffer_total_bytes"])
        self.assertEqual(parsed["network"]["enp1s0f0np0"]["mtu"], 9000)
        self.assertEqual(
            parsed["network"]["enp1s0f0np0"]["rdma_device"], "rocep1s0f0"
        )
        self.assertEqual(parsed["network"]["enp1s0f0np0"]["rx_packets"], 10)
        self.assertEqual(parsed["thermals"]["cpu_cluster_max_c"], 54.8)
        self.assertEqual(parsed["thermals"]["soc_c"], 51.2)
        self.assertEqual(parsed["thermals"]["nvme_composite_c"], 41.85)

    def test_headless_worker_state_uses_process_and_host_health(self):
        active = dashboard.Collector.worker_state({"vllm_rss_bytes": 4096})
        self.assertTrue(active["healthy"])
        self.assertTrue(active["headless"])
        self.assertEqual(active["state"], "headless_worker")
        self.assertEqual(active["metrics_scope"], "none")
        self.assertEqual(active["rates"], {})

        stopped = dashboard.Collector.worker_state({"vllm_rss_bytes": 0})
        self.assertFalse(stopped["healthy"])
        self.assertEqual(stopped["state"], "worker_stopped")

        unreachable = dashboard.Collector.worker_state(
            {"error": "ssh timeout", "vllm_rss_bytes": 4096}
        )
        self.assertFalse(unreachable["healthy"])
        self.assertEqual(unreachable["state"], "unreachable")
        self.assertEqual(unreachable["error"], "ssh timeout")

    def test_tp_worker_outage_is_down_and_timer_survives_recovery(self):
        collector = make_collector()
        nodes = {
            "cerberus1": {
                "role": "aggregate",
                "system": {},
                "vllm": {"healthy": True, "state": "serving"},
            },
            "cerberus2": {
                "role": "worker",
                "system": {"error": "ssh timeout"},
                "vllm": {
                    "healthy": False,
                    "state": "unreachable",
                    "error": "ssh timeout",
                },
            },
        }
        for name, node in nodes.items():
            node["health"] = collector.node_health(name, node)
        endpoint = {
            "healthy": True,
            "state": "serving",
            "mode": "direct",
            "label": "TP2 aggregate endpoint",
            "url": "http://rank0:8000",
            "active_ranks": 1,
            "expected_ranks": 2,
        }

        initial = collector.cluster_health(nodes, endpoint, 1_000)
        later = collector.cluster_health(nodes, endpoint, 1_012)

        self.assertEqual(initial["state"], "down")
        self.assertFalse(initial["healthy"])
        self.assertTrue(initial["endpoint_healthy"])
        self.assertEqual(initial["affected_nodes"], ["cerberus2"])
        self.assertIn("cannot be reached over SSH", initial["reason"])
        self.assertEqual(initial["outage_started_at"], "1970-01-01T00:16:40Z")
        self.assertEqual(later["outage_started_at"], initial["outage_started_at"])
        self.assertEqual(later["outage_elapsed_seconds"], 12)
        self.assertIsNone(later["recovery_started_at"])

        nodes["cerberus2"]["system"] = {"vllm_rss_bytes": 1024}
        nodes["cerberus2"]["vllm"] = {
            "healthy": True,
            "state": "headless_worker",
        }
        nodes["cerberus2"]["health"] = collector.node_health(
            "cerberus2", nodes["cerberus2"]
        )
        endpoint["active_ranks"] = 2

        recovery_one = collector.cluster_health(nodes, endpoint, 1_014)
        recovery_two = collector.cluster_health(nodes, endpoint, 1_016)
        serving = collector.cluster_health(nodes, endpoint, 1_018)

        self.assertEqual(recovery_one["state"], "recovering")
        self.assertTrue(recovery_one["healthy"])
        self.assertEqual(recovery_one["affected_nodes"], ["cerberus2"])
        self.assertEqual(
            recovery_one["outage_started_at"], initial["outage_started_at"]
        )
        self.assertEqual(recovery_one["outage_elapsed_seconds"], 14)
        self.assertEqual(
            recovery_one["recovery_started_at"], "1970-01-01T00:16:54Z"
        )
        self.assertEqual(recovery_two["state"], "recovering")
        self.assertEqual(recovery_two["outage_elapsed_seconds"], 14)
        self.assertEqual(serving["state"], "serving")
        self.assertEqual(serving["affected_nodes"], [])
        self.assertIsNone(serving["outage_started_at"])
        self.assertIsNone(serving["outage_elapsed_seconds"])
        self.assertIsNone(serving["recovery_started_at"])

    def test_endpoint_failure_is_prominent_even_if_worker_is_alive(self):
        collector = make_collector()
        nodes = {
            "cerberus1": {
                "role": "aggregate",
                "system": {},
                "vllm": {
                    "healthy": False,
                    "state": "unreachable",
                    "error": "connection refused",
                },
            },
            "cerberus2": {
                "role": "worker",
                "system": {"vllm_rss_bytes": 1024},
                "vllm": {"healthy": True, "state": "headless_worker"},
            },
        }
        for name, node in nodes.items():
            node["health"] = collector.node_health(name, node)
        endpoint = {
            "healthy": False,
            "state": "unreachable",
            "mode": "direct",
            "label": "TP2 aggregate endpoint",
            "url": "http://rank0:8000",
            "error": "connection refused",
            "active_ranks": 1,
            "expected_ranks": 2,
        }

        status = collector.cluster_health(nodes, endpoint, 2_000)

        self.assertEqual(status["state"], "down")
        self.assertFalse(status["endpoint_healthy"])
        self.assertEqual(status["endpoint"]["url"], "http://rank0:8000")
        self.assertIn("TP2 aggregate endpoint is unreachable", status["reason"])
        self.assertIn("cerberus1", status["affected_nodes"])
        self.assertEqual(status["outage_elapsed_seconds"], 0)

    def test_router_with_one_failed_replica_is_degraded(self):
        collector = make_collector(
            node_roles={"cerberus1": "replica", "cerberus2": "replica"},
            inference_mode="router",
        )
        nodes = {
            "cerberus1": {
                "role": "replica",
                "system": {},
                "vllm": {"healthy": True, "state": "serving"},
            },
            "cerberus2": {
                "role": "replica",
                "system": {},
                "vllm": {
                    "healthy": False,
                    "state": "unreachable",
                    "error": "timed out",
                },
            },
        }
        for name, node in nodes.items():
            node["health"] = collector.node_health(name, node)
        endpoint = {
            "healthy": True,
            "state": "routing",
            "mode": "router",
            "label": "Router",
            "url": "http://router:8080",
            "active_workers": 1,
        }

        status = collector.cluster_health(nodes, endpoint, 3_000)

        self.assertEqual(status["state"], "degraded")
        self.assertFalse(status["healthy"])
        self.assertTrue(status["endpoint_healthy"])
        self.assertEqual(status["affected_nodes"], ["cerberus2"])
        self.assertEqual(status["outage_elapsed_seconds"], 0)

    def test_optional_spark1_remote_probe_uses_strict_known_hosts(self):
        collector = make_collector(
            spark1_host="operator@cerberus1.lan",
            spark1_ssh_key="/cerberus1-key",
            ssh_known_hosts="/dashboard/known_hosts",
        )
        completed = mock.Mock(
            returncode=0,
            stdout="HOSTNAME=cerberus1\nVLLM_RSS=100\n",
            stderr="",
        )

        with mock.patch.object(dashboard.subprocess, "run", return_value=completed) as run:
            result = collector.spark1_system()

        command = run.call_args.args[0]
        self.assertEqual(result["hostname"], "cerberus1")
        self.assertIn("/cerberus1-key", command)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("UserKnownHostsFile=/dashboard/known_hosts", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertIn("operator@cerberus1.lan", command)

    def test_spark1_probe_remains_local_when_host_is_unset(self):
        collector = make_collector(spark1_host=None)
        with mock.patch.object(
            collector, "local_system", return_value={"hostname": "cerberus1-local"}
        ) as local:
            result = collector.spark1_system()
        local.assert_called_once_with()
        self.assertEqual(result["hostname"], "cerberus1-local")

    def test_aggregate_metrics_take_precedence_and_worker_is_never_counted(self):
        nodes = {
            "cerberus1": {
                "role": "aggregate",
                "vllm": {
                    "rates": {
                        "prompt_tokens_per_second": 11,
                        "generation_tokens_per_second": 23,
                    }
                },
            },
            "cerberus2": {
                "role": "worker",
                "vllm": {
                    "rates": {
                        "prompt_tokens_per_second": 999,
                        "generation_tokens_per_second": 999,
                    }
                },
            },
            "legacy-replica": {
                "role": "replica",
                "vllm": {
                    "rates": {
                        "prompt_tokens_per_second": 7,
                        "generation_tokens_per_second": 13,
                    }
                },
            },
        }
        endpoint = {}
        dashboard.Collector.add_backend_rates(endpoint, nodes)
        self.assertEqual(endpoint["metrics_source_nodes"], ["cerberus1"])
        self.assertEqual(endpoint["backend_prompt_tokens_per_second"], 11)
        self.assertEqual(endpoint["backend_generation_tokens_per_second"], 23)

    def test_replica_metrics_are_summed_when_there_is_no_aggregate(self):
        nodes = {
            "cerberus1": {
                "role": "replica",
                "vllm": {
                    "rates": {
                        "prompt_tokens_per_second": 2,
                        "generation_tokens_per_second": 3,
                    }
                },
            },
            "cerberus2": {
                "role": "replica",
                "vllm": {
                    "rates": {
                        "prompt_tokens_per_second": 5,
                        "generation_tokens_per_second": 7,
                    }
                },
            },
        }
        endpoint = {}
        dashboard.Collector.add_backend_rates(endpoint, nodes)
        self.assertEqual(endpoint["metrics_source_nodes"], ["cerberus1", "cerberus2"])
        self.assertEqual(endpoint["backend_prompt_tokens_per_second"], 7)
        self.assertEqual(endpoint["backend_generation_tokens_per_second"], 10)

    def test_tp2_collection_does_not_probe_worker_or_router(self):
        class FakeCollector(dashboard.Collector):
            def __init__(self):
                super().__init__(
                    spark2_host="cerberus2",
                    ssh_key="/unused",
                    node_urls={
                        "cerberus1": "http://rank0:8000",
                        "cerberus2": "http://rank1:8000",
                    },
                    node_roles={"cerberus1": "aggregate", "cerberus2": "worker"},
                    inference_mode="direct",
                    router_url="http://unused:8080",
                    router_metrics_url="http://unused:29000",
                    interfaces=(),
                    interval=2,
                )
                self.vllm_calls = []

            @staticmethod
            def local_system():
                return {
                    "hostname": "cerberus1",
                    "vllm_rss_bytes": 1000,
                    "network": {},
                }

            @staticmethod
            def remote_system():
                return {
                    "hostname": "cerberus2",
                    "vllm_rss_bytes": 2000,
                    "network": {},
                }

            def vllm_metrics(self, base_url):
                self.vllm_calls.append(base_url)
                return {
                    "healthy": True,
                    "state": "serving",
                    "counters": {
                        "prompt_tokens": 120,
                        "generation_tokens": 230,
                        "requests": 14,
                    },
                }

            @staticmethod
            def router_metrics():
                raise AssertionError("direct mode must not probe the router")

        collector = FakeCollector()
        collector.snapshot = {
            "_sample_time": time.time() - 2,
            "nodes": {
                "cerberus1": {
                    "system": {"network": {}},
                    "vllm": {
                        "counters": {
                            "prompt_tokens": 100,
                            "generation_tokens": 200,
                            "requests": 10,
                        }
                    },
                },
                "cerberus2": {"system": {"network": {}}},
            },
            "router": {},
        }
        collector.collect()
        snapshot = collector.get_snapshot()

        self.assertEqual(collector.vllm_calls, ["http://rank0:8000"])
        worker = snapshot["nodes"]["cerberus2"]
        self.assertEqual(worker["role"], "worker")
        self.assertIsNone(worker["endpoint"])
        self.assertTrue(worker["vllm"]["healthy"])
        self.assertEqual(worker["vllm"]["state"], "headless_worker")
        self.assertEqual(snapshot["router"]["mode"], "direct")
        self.assertEqual(snapshot["router"]["metrics_source_nodes"], ["cerberus1"])
        self.assertEqual(
            snapshot["router"]["backend_generation_tokens_per_second"],
            snapshot["nodes"]["cerberus1"]["vllm"]["rates"][
                "generation_tokens_per_second"
            ],
        )
        self.assertEqual(len(snapshot["history"]), 1)
        self.assertIn("generation_tokens_per_second", snapshot["history"][0])
        self.assertIn("cerberus1", snapshot["history"][0]["nodes"])
        self.assertNotIn("spark1", snapshot["nodes"])
        self.assertNotIn("spark2", snapshot["history"][0]["nodes"])


if __name__ == "__main__":
    unittest.main()
