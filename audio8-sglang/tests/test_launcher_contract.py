from __future__ import annotations

import os
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LauncherContractTests(unittest.TestCase):
    def test_provenance_cache_and_loopback_contracts_are_wired(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        build = (ROOT / "build-image.sh").read_text(encoding="utf-8")
        launcher = (ROOT / "run-backend.sh").read_text(encoding="utf-8")
        for label in (
            "io.cerberus.audio8-sglang.base-image",
            "io.cerberus.audio8-sglang.sglang-omni-commit",
            "io.cerberus.audio8-sglang.audio8-tts-commit",
            "io.cerberus.audio8-sglang.source-contract-patchset-sha256",
        ):
            self.assertIn(label, dockerfile)
        self.assertIn("AUDIO8_RUNTIME_FINGERPRINT=${values[7]}", build)
        self.assertIn("runtime_identity.py\" verify-labels", build)
        self.assertIn("runtime_identity.py\" verify-labels", launcher)
        self.assertIn("prepare_cache.py", launcher)
        self.assertIn("/${runtime_fingerprint}}", launcher)
        self.assertIn('--publish "127.0.0.1:${backend_port}:8010"', launcher)

    def test_experimental_backend_rejects_every_nontrial_port(self) -> None:
        for port in ("1", "8010", "8020", "18011", "65535"):
            environment = dict(os.environ)
            environment.update(
                AUDIO8_SGLANG_EXPERIMENTAL="1",
                AUDIO8_SGLANG_BACKEND_PORT=port,
            )
            completed = subprocess.run(
                [str(ROOT / "run-backend.sh")],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            with self.subTest(port=port):
                self.assertEqual(completed.returncode, 2)
                self.assertIn("locked to 18010", completed.stderr)

    def test_production_backend_is_private_offline_and_supervised(self) -> None:
        launcher = (ROOT / "run-production-backend.sh").read_text(
            encoding="utf-8"
        )
        supervisor = (ROOT / "supervise-production-backend.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("--publish", launcher)
        self.assertIn("cerberus3-audio8-sglang-backend", launcher)
        self.assertIn('--gpus device=0', launcher)
        self.assertIn('HF_HUB_OFFLINE=1', launcher)
        self.assertIn('TRANSFORMERS_OFFLINE=1', launcher)
        self.assertIn(
            '"${cache_dir}" "${image_fingerprint}" "${runtime_uid}"', launcher
        )
        self.assertIn("check_health.py backend", launcher)
        self.assertIn("AUDIO8_TTS_MAX_RUNNING_REQUESTS=2", launcher)
        self.assertIn("AUDIO8_TTS_ENABLE_TORCH_COMPILE=1", launcher)
        self.assertIn("docker stop --time 30", supervisor)
        self.assertIn("unhealthy_polls >= 3", supervisor)
        self.assertIn("systemd-notify", supervisor)
        self.assertIn("--log-opt max-size=10m", launcher)
        self.assertIn('"${root}/wait-for-docker.sh"', launcher)

    def test_production_gateway_is_the_only_public_listener(self) -> None:
        launcher = (ROOT / "run-production-gateway.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--publish 127.0.0.1:8010:8010", launcher)
        self.assertNotIn("--publish 0.0.0.0:8010:8010", launcher)
        self.assertIn("--init", launcher)
        self.assertIn("AUDIO8_SGLANG_PRODUCTION=1", launcher)
        self.assertIn("http://172.30.82.2:8010/v1/audio/speech", launcher)
        self.assertIn("--dns 127.0.0.1", launcher)
        self.assertIn("name=${frontend_network},ip=172.30.81.2", launcher)
        self.assertIn("name=${backend_network},ip=172.30.82.3", launcher)
        self.assertNotIn("/references", launcher)
        self.assertNotIn("/models", launcher)
        self.assertNotIn("--gpus", launcher)
        self.assertIn("--log-opt max-size=10m", launcher)
        supervisor = (ROOT / "supervise-production-gateway.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('backend_health', supervisor)
        self.assertIn('unhealthy_polls >= 3', supervisor)
        self.assertIn('systemd-notify', supervisor)

    def test_production_units_preserve_stock_rollback(self) -> None:
        gateway_unit = (
            ROOT / "systemd/cerberus3-audio8-sglang-gateway.service"
        ).read_text(encoding="utf-8")
        backend_unit = (
            ROOT / "systemd/cerberus3-audio8-sglang-backend.service"
        ).read_text(encoding="utf-8")
        installer = (ROOT / "install-production.sh").read_text(encoding="utf-8")
        cutover = (ROOT / "cutover-production.sh").read_text(encoding="utf-8")
        rollback = (ROOT / "rollback-to-stock.sh").read_text(encoding="utf-8")
        self.assertNotIn("Conflicts=cerberus3-audio8.service", gateway_unit)
        self.assertIn("Wants=", gateway_unit)
        self.assertNotIn("Requires=cerberus3-audio8-sglang-backend", gateway_unit)
        self.assertIn("Type=notify", backend_unit)
        self.assertIn("Type=notify", gateway_unit)
        self.assertIn("/usr/sbin:/sbin", backend_unit)
        self.assertIn("/usr/sbin:/sbin", gateway_unit)
        self.assertIn("AF_INET6 AF_NETLINK", gateway_unit)
        self.assertIn("WatchdogSec=30s", gateway_unit)
        self.assertIn("supervise-production-gateway.sh", gateway_unit)
        self.assertIn("WatchdogSec=30s", backend_unit)
        self.assertIn(
            "Requires=cerberus3-audio8-sglang-network.service", backend_unit
        )
        self.assertIn(
            "ExecStartPre=+/usr/local/lib/cerberus3-audio8-sglang/"
            "ensure-production-networks.sh",
            backend_unit,
        )
        self.assertIn("create-rollback-snapshot.sh", installer)
        self.assertIn('systemd/backend.conf', installer)
        self.assertTrue((ROOT / "systemd/backend.conf").is_file())
        backend_config = (ROOT / "systemd/backend.conf").read_text(
            encoding="utf-8"
        )
        private_reference = (
            "/home/catid/.local/share/audio8/computer-voice-20260812"
        )
        self.assertIn(
            f"AUDIO8_REFERENCE_DIR={private_reference}", backend_config
        )
        self.assertIn(private_reference, backend_unit)
        self.assertNotIn("/audio8/authorized-voice", backend_config)
        self.assertNotIn("/audio8/authorized-voice", backend_unit)
        self.assertIn("rollback-to-stock.sh", installer)
        self.assertIn('"${runtime_root}/systemd"', installer)
        self.assertNotIn("disable --now cerberus3-audio8.service", installer)
        self.assertIn(
            "for obsolete in 50-audio8-sglang-voice-bridge.conf", installer
        )
        self.assertIn("50-audio8-sglang-voice-target.conf", installer)
        self.assertIn('gateway_source="${root}/systemd/', cutover)
        self.assertIn('"${unit_root}/${stock_unit}"', cutover)
        self.assertIn("systemctl daemon-reload", cutover)
        self.assertNotIn('disable --now "${stock_unit}"', cutover)
        self.assertIn('systemctl stop "${stock_unit}"', cutover)
        self.assertIn("atomic_install_unit", cutover)
        self.assertIn("mutation_started", cutover)
        self.assertIn("validate-rollback-snapshot.sh", cutover)
        self.assertIn('systemctl is-enabled --quiet "${stock_unit}"', cutover)
        self.assertNotIn("50-audio8-sglang", cutover)
        self.assertIn('"${rollback_root}/cerberus3-audio8.service"', rollback)
        self.assertNotIn('disable --now "${stock_unit}"', rollback)
        self.assertIn("atomic_install_unit", rollback)
        self.assertIn("check_stock_health.py", rollback)
        self.assertIn("stock-enabled-state", rollback)
        self.assertIn("cerberus3-audio8-stock-rollback-v2", rollback)
        self.assertNotIn("cerberus3-voice-stack.target", rollback)

    def test_frontend_egress_is_fail_closed(self) -> None:
        network = (ROOT / "ensure-production-networks.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("--internal", network)
        self.assertIn("CERBERUS-AUDIO8", network)
        self.assertIn("ESTABLISHED,RELATED", network)
        self.assertIn("-j REJECT", network)
        self.assertIn("gateway_frontend_ip=172.30.81.2", network)
        self.assertIn("backend_ip=172.30.82.2", network)
        self.assertIn("gateway_backend_ip=172.30.82.3", network)
        self.assertIn('"${backend_ip}/32"', network)
        self.assertIn('"${gateway_frontend_ip}/32"', network)
        self.assertIn("CERBERUS-AUDIO8-HOST", network)
        self.assertIn("iptables -C OUTPUT", network)
        self.assertIn(
            'iptables -C DOCKER-USER -s "${gateway_backend_ip}/32"', network
        )
        self.assertIn(
            'iptables -C DOCKER-USER -s "${gateway_frontend_ip}/32"', network
        )

    def test_rollback_snapshot_is_transactional_and_self_contained(self) -> None:
        creator = (ROOT / "create-rollback-snapshot.sh").read_text(
            encoding="utf-8"
        )
        validator = (ROOT / "validate-rollback-snapshot.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("stock-rollback-v2", creator)
        self.assertIn("mktemp -d", creator)
        self.assertIn("mv -T", creator)
        self.assertIn("audio8/run-server.sh", creator)
        self.assertIn("audio8/MODEL.lock.json", creator)
        self.assertIn("SHA256SUMS", creator)
        self.assertIn("stock-rollback-v2", validator)
        self.assertIn("sha256sum --strict --check", validator)
        self.assertIn("stock-image-id", validator)

    def test_services_wait_and_retry_across_docker_outages(self) -> None:
        backend_unit = (
            ROOT / "systemd/cerberus3-audio8-sglang-backend.service"
        ).read_text(encoding="utf-8")
        gateway_unit = (
            ROOT / "systemd/cerberus3-audio8-sglang-gateway.service"
        ).read_text(encoding="utf-8")
        network_unit = (
            ROOT / "systemd/cerberus3-audio8-sglang-network.service"
        ).read_text(encoding="utf-8")
        for unit in (backend_unit, gateway_unit, network_unit):
            self.assertIn("StartLimitIntervalSec=0", unit)
            self.assertIn("TimeoutStartSec=0", unit)
        waiter = (ROOT / "wait-for-docker.sh").read_text(encoding="utf-8")
        self.assertIn("while ! docker info", waiter)
        self.assertIn("sleep 5", waiter)


if __name__ == "__main__":
    unittest.main()
