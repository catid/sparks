from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from voice_assistant import alarm_service


class UnixHttpConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


class AlarmStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = os.path.join(self.temporary.name, "alarms.sqlite3")
        self.store = alarm_service.AlarmStore(self.database, "America/Chicago")
        self.now = 1_786_460_400.0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_timer_is_persistent_listed_and_cancelled(self) -> None:
        created = self.store.create(
            {"kind": "timer", "duration_seconds": 90, "label": "  pasta   pot "},
            self.now,
        )
        self.assertEqual(created["kind"], "timer")
        self.assertEqual(created["label"], "pasta pot")
        self.assertEqual(created["status"], "pending")
        self.assertEqual(created["timezone"], "America/Chicago")
        self.assertEqual(self.store.list_active(), [created])

        reopened = alarm_service.AlarmStore(self.database, "America/Chicago")
        self.assertEqual(reopened.list_active(), [created])
        cancelled = reopened.cancel(created["id"], self.now + 1)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(reopened.list_active(), [])
        self.assertEqual(pathlib.Path(self.database).stat().st_mode & 0o777, 0o600)

    def test_alarm_requires_an_offset_and_future_bounded_time(self) -> None:
        with self.assertRaisesRegex(alarm_service.RequestError, "UTC offset"):
            self.store.create(
                {"kind": "alarm", "due_at": "2026-08-11T17:30:00"}, self.now
            )
        with self.assertRaisesRegex(alarm_service.RequestError, "future"):
            self.store.create(
                {"kind": "alarm", "due_at": "2020-01-01T00:00:00-05:00"}, self.now
            )
        with self.assertRaisesRegex(alarm_service.RequestError, "366 days"):
            self.store.create(
                {"kind": "alarm", "due_at": "2030-01-01T00:00:00-06:00"}, self.now
            )

    def test_due_alarm_repeats_until_dismissed(self) -> None:
        created = self.store.create(
            {"kind": "timer", "duration_seconds": 1}, self.now
        )
        first = self.store.due_to_ring(self.now + 1.1)
        self.assertEqual([event["id"] for event in first], [created["id"]])
        self.assertEqual(first[0]["status"], "ringing")
        self.assertEqual(self.store.due_to_ring(self.now + 2), [])
        repeated = self.store.due_to_ring(
            self.now + 1.1 + alarm_service.RING_INTERVAL_SECONDS
        )
        self.assertEqual([event["id"] for event in repeated], [created["id"]])
        dismissed = self.store.dismiss_ringing(None)
        self.assertEqual(dismissed[0]["status"], "dismissed")
        self.assertEqual(self.store.list_active(), [])

    def test_stale_missed_alarm_expires_instead_of_ringing_after_restart(self) -> None:
        created = self.store.create(
            {"kind": "timer", "duration_seconds": 1}, self.now
        )
        events = self.store.due_to_ring(
            self.now + alarm_service.MAX_RINGING_SECONDS + 2
        )
        self.assertEqual(events, [])
        self.assertEqual(self.store.list_active(), [])
        expired = self.store.cancel(created["id"], self.now)
        self.assertEqual(expired["status"], "expired")

    def test_rejects_unknown_fields_controls_and_mixed_schedule_types(self) -> None:
        bad_payloads = [
            {"kind": "timer", "duration_seconds": 1, "other": True},
            {"kind": "timer", "duration_seconds": True},
            {"kind": "timer", "duration_seconds": 1, "due_at": "x"},
            {"kind": "alarm", "due_at": "2026-08-12T17:00:00-05:00", "duration_seconds": 1},
            {"kind": "timer", "duration_seconds": 1, "label": "bad\nlabel"},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload), self.assertRaises(alarm_service.RequestError):
                self.store.create(payload, self.now)


class AlarmApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.socket_path = os.path.join(self.temporary.name, "api.sock")
        self.store = alarm_service.AlarmStore(
            os.path.join(self.temporary.name, "alarms.sqlite3"), "America/Chicago"
        )
        self.server = alarm_service.AlarmHttpServer(
            self.socket_path, self.store, clock=lambda: 1_786_460_400.0
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, body=None):
        connection = UnixHttpConnection(self.socket_path)
        encoded = None if body is None else json.dumps(body)
        connection.request(
            method,
            path,
            body=encoded,
            headers={"Content-Type": "application/json"} if encoded is not None else {},
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_create_list_cancel_and_health_over_owner_socket(self) -> None:
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["timezone"], "America/Chicago")
        self.assertEqual(pathlib.Path(self.socket_path).stat().st_mode & 0o777, 0o600)

        status, payload = self.request(
            "POST", "/v1/alarms", {"kind": "timer", "duration_seconds": 30}
        )
        self.assertEqual(status, 201)
        alarm_id = payload["alarm"]["id"]
        status, listing = self.request("GET", "/v1/alarms")
        self.assertEqual(status, 200)
        self.assertEqual(listing["alarms"][0]["id"], alarm_id)
        status, cancelled = self.request(
            "POST", f"/v1/alarms/{alarm_id}/cancel", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["alarm"]["status"], "cancelled")

    def test_invalid_request_returns_bounded_error(self) -> None:
        status, payload = self.request(
            "POST", "/v1/alarms", {"kind": "timer", "duration_seconds": 0}
        )
        self.assertEqual(status, 400)
        self.assertIn("between 1", payload["error"])
        status, payload = self.request("GET", "/not-found")
        self.assertEqual(status, 404)
        self.assertEqual(payload, {"error": "not found"})

    def test_trickled_headers_have_a_total_deadline(self) -> None:
        with mock.patch.object(alarm_service, "HEADER_TIMEOUT_SECONDS", 0.15):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(self.socket_path)
            self.addCleanup(client.close)
            started = time.monotonic()
            client.sendall(b"POST /v1/alarms HTTP/1.1\r\nX-Slow: ")
            for _ in range(8):
                time.sleep(0.04)
                try:
                    client.sendall(b"x")
                except OSError:
                    break
            try:
                response = client.recv(1)
            except ConnectionResetError:
                response = b""
            self.assertEqual(response, b"")
            self.assertLess(time.monotonic() - started, 0.7)

    def test_trickled_body_has_a_total_deadline(self) -> None:
        with mock.patch.object(alarm_service, "BODY_TIMEOUT_SECONDS", 0.15):
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(self.socket_path)
            self.addCleanup(client.close)
            client.sendall(
                b"POST /v1/alarms HTTP/1.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{"
            )
            started = time.monotonic()
            time.sleep(0.3)
            try:
                response = client.recv(4096)
            except ConnectionResetError:
                response = b""
            if response:
                self.assertIn(b" 408 ", response)
            self.assertLess(time.monotonic() - started, 0.7)

    def test_connection_admission_is_bounded_before_thread_creation(self) -> None:
        socket_path = os.path.join(self.temporary.name, "bounded.sock")
        server = alarm_service.AlarmHttpServer(
            socket_path,
            self.store,
            max_connections=1,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        blocker.settimeout(1)
        blocker.connect(socket_path)
        blocker.sendall(b"GET /health HTTP/1.1\r\nX-Slow: ")
        time.sleep(0.05)
        rejected = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        rejected.settimeout(1)
        rejected.connect(socket_path)
        try:
            response = rejected.recv(4096)
            self.assertIn(b" 503 ", response)
            self.assertIn(b"Retry-After: 1", response)
        finally:
            rejected.close()
            blocker.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_health_is_degraded_when_scheduler_is_unhealthy(self) -> None:
        scheduler = mock.Mock()
        scheduler.status.return_value = {"status": "error", "healthy": False}
        self.server.scheduler = scheduler
        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["scheduler"], "error")

    def test_response_chunks_share_one_total_write_deadline(self) -> None:
        class SlowWriter:
            def write(self, _payload):
                time.sleep(0.04)

        handler = object.__new__(alarm_service.AlarmRequestHandler)
        handler.connection = mock.Mock()
        handler.wfile = SlowWriter()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "total deadline"):
            handler._write_body_with_deadline(
                b"x" * (2 * 65_536 + 1),
                started + 0.07,
            )
        self.assertLess(time.monotonic() - started, 0.2)


class AlarmAudioTests(unittest.TestCase):
    def settings(self, tts_url: str) -> alarm_service.AlarmSettings:
        return alarm_service.AlarmSettings(
            database_path="/tmp/test-alarms.sqlite3",
            socket_path="/tmp/test-alarm.sock",
            timezone_name="America/Chicago",
            tts_url=tts_url,
            tts_model="audio8/tts-0.6b",
            playback_device="test",
            playback_lock_path="/tmp/test-playback.lock",
        )

    def test_alarm_cue_and_announcements_are_bounded(self) -> None:
        cue = alarm_service.alarm_cue()
        duration = alarm_service.validate_wav(cue)
        self.assertGreater(duration, 1)
        self.assertLess(duration, 2)
        self.assertEqual(
            alarm_service.AlarmAudio.announcement(
                {"kind": "timer", "label": "pasta"}
            ),
            "Your pasta timer is done.",
        )
        self.assertEqual(
            alarm_service.AlarmAudio.announcement({"kind": "alarm", "label": None}),
            "Your alarm is ringing.",
        )

    def test_scheduler_claims_and_rings_due_events(self) -> None:
        stop_event = threading.Event()
        store = mock.Mock()
        store.due_to_ring.side_effect = [[{"kind": "timer", "label": None}], []]
        audio = mock.Mock()
        scheduler = alarm_service.AlarmScheduler(store, audio, stop_event)

        def ring(_event):
            stop_event.set()

        audio.ring.side_effect = ring
        scheduler.start()
        scheduler.join(timeout=2)
        self.assertFalse(scheduler.is_alive())
        audio.ring.assert_called_once()

    def test_scheduler_recovers_from_store_failure_and_reports_health(self) -> None:
        stop_event = threading.Event()
        failed = threading.Event()
        allow_recovery = threading.Event()

        class FlakyStore:
            calls = 0

            def due_to_ring(self, _now):
                self.calls += 1
                if self.calls == 1:
                    failed.set()
                    raise sqlite3.OperationalError("private database detail")
                allow_recovery.wait(1)
                return []

        store = FlakyStore()
        scheduler = alarm_service.AlarmScheduler(store, mock.Mock(), stop_event)
        scheduler.start()
        self.assertTrue(failed.wait(1))
        deadline = time.monotonic() + 1
        while scheduler.status()["status"] != "error" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(scheduler.status(), {"status": "error", "healthy": False})
        self.assertFalse(scheduler.ready_event.is_set())
        allow_recovery.set()
        self.assertTrue(scheduler.ready_event.wait(1))
        self.assertEqual(scheduler.status(), {"status": "ok", "healthy": True})
        stop_event.set()
        scheduler.join(timeout=1)
        self.assertFalse(scheduler.is_alive())

    def test_audio8_response_has_one_total_monotonic_deadline(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        release = threading.Event()
        wav = alarm_service.alarm_cue()

        def serve() -> None:
            connection, _address = listener.accept()
            with connection:
                connection.recv(4096)
                connection.sendall(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(wav)}\r\n\r\n".encode(
                        "ascii"
                    )
                    + wav[:1]
                )
                release.wait(1)

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        audio = alarm_service.AlarmAudio(
            self.settings(f"http://127.0.0.1:{listener.getsockname()[1]}/speech")
        )
        started = time.monotonic()
        try:
            with mock.patch.object(alarm_service, "TTS_TIMEOUT_SECONDS", 0.15):
                with self.assertRaisesRegex(TimeoutError, "total deadline"):
                    audio.synthesize("hello")
            self.assertLess(time.monotonic() - started, 0.7)
        finally:
            release.set()
            listener.close()
            server_thread.join(timeout=1)

    def test_audio8_request_is_cancelled_during_shutdown(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        accepted = threading.Event()
        release = threading.Event()

        def serve() -> None:
            connection, _address = listener.accept()
            with connection:
                connection.recv(4096)
                accepted.set()
                release.wait(1)

        server_thread = threading.Thread(target=serve, daemon=True)
        server_thread.start()
        stop_event = threading.Event()
        audio = alarm_service.AlarmAudio(
            self.settings(f"http://127.0.0.1:{listener.getsockname()[1]}/speech"),
            stop_event,
        )
        errors: list[BaseException] = []

        def synthesize() -> None:
            try:
                audio.synthesize("hello")
            except BaseException as error:
                errors.append(error)

        client_thread = threading.Thread(target=synthesize)
        client_thread.start()
        self.assertTrue(accepted.wait(1))
        started = time.monotonic()
        stop_event.set()
        client_thread.join(timeout=0.7)
        try:
            self.assertFalse(client_thread.is_alive())
            self.assertLess(time.monotonic() - started, 0.7)
            self.assertEqual(len(errors), 1)
            self.assertIs(type(errors[0]), InterruptedError)
        finally:
            release.set()
            listener.close()
            server_thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
