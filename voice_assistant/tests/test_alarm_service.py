from __future__ import annotations

import http.client
import json
import os
import pathlib
import socket
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


class AlarmAudioTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
