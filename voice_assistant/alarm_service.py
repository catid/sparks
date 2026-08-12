#!/usr/bin/env python3
"""Persistent, local-only timers and alarms for the Cerberus speaker."""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import math
import os
import re
import secrets
import signal
import sqlite3
import struct
import subprocess
import socket
import threading
import time
import urllib.parse
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingUnixStreamServer
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from voice_assistant.voice_bridge import acquire_playback_lock
except ModuleNotFoundError:
    from voice_bridge import acquire_playback_lock


MAX_BODY_BYTES = 8 * 1024
MAX_LABEL_CHARACTERS = 80
MAX_TIMER_SECONDS = 7 * 24 * 60 * 60
MAX_ALARM_HORIZON_SECONDS = 366 * 24 * 60 * 60
MAX_LIST_RESULTS = 100
RING_INTERVAL_SECONDS = 20
MAX_RINGING_SECONDS = 10 * 60
MAX_TTS_BYTES = 32 * 1024 * 1024
MAX_HTTP_CONNECTIONS = 16
HEADER_TIMEOUT_SECONDS = 5.0
BODY_TIMEOUT_SECONDS = 5.0
WRITE_TIMEOUT_SECONDS = 5.0
TTS_TIMEOUT_SECONDS = 120.0
SCHEDULER_READY_TIMEOUT_SECONDS = 10.0
LABEL_RE = re.compile(r"^[^\x00-\x1f\x7f]*$")


class RequestError(ValueError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def utc_timestamp(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def normalize_label(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequestError(400, "label must be a string")
    if not LABEL_RE.fullmatch(value):
        raise RequestError(
            400,
            f"label must contain at most {MAX_LABEL_CHARACTERS} printable characters",
        )
    label = " ".join(value.split())
    if not label:
        return None
    if len(label) > MAX_LABEL_CHARACTERS:
        raise RequestError(
            400,
            f"label must contain at most {MAX_LABEL_CHARACTERS} printable characters",
        )
    return label


def parse_due_at(value: Any, now: float) -> float:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise RequestError(400, "due_at must be an ISO 8601 timestamp with a UTC offset")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise RequestError(400, "due_at must be a valid ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RequestError(400, "due_at must include a UTC offset")
    due_at = parsed.timestamp()
    if not math.isfinite(due_at) or due_at <= now:
        raise RequestError(400, "due_at must be in the future")
    if due_at - now > MAX_ALARM_HORIZON_SECONDS:
        raise RequestError(400, "due_at must be within 366 days")
    return due_at


@dataclass(frozen=True)
class AlarmSettings:
    database_path: str
    socket_path: str
    timezone_name: str
    tts_url: str
    tts_model: str
    playback_device: str
    playback_lock_path: str

    @classmethod
    def from_environment(cls) -> "AlarmSettings":
        state_dir = os.environ.get("ALARM_STATE_DIR", "/var/lib/cerberus3-alarms")
        runtime_dir = os.environ.get("ALARM_RUNTIME_DIR", "/run/cerberus3-alarms")
        timezone_name = os.environ.get("ALARM_TIMEZONE", "America/Chicago").strip()
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise RuntimeError("ALARM_TIMEZONE is not available") from error
        values = {
            "database_path": os.path.join(state_dir, "alarms.sqlite3"),
            "socket_path": os.path.join(runtime_dir, "api.sock"),
            "timezone_name": timezone_name,
            "tts_url": os.environ.get(
                "ALARM_TTS_URL", "http://127.0.0.1:8010/v1/audio/speech"
            ),
            "tts_model": os.environ.get("ALARM_TTS_MODEL", "audio8/tts-0.6b"),
            "playback_device": os.environ.get(
                "ALARM_PLAYBACK_DEVICE", "plughw:CARD=CP900,DEV=0"
            ),
            "playback_lock_path": os.environ.get(
                "ALARM_PLAYBACK_LOCK_PATH",
                "/var/lib/cerberus3-alarms/playback.lock",
            ),
        }
        for name in ("database_path", "socket_path", "playback_lock_path"):
            value = values[name]
            if not os.path.isabs(value) or len(value) > 512:
                raise RuntimeError(f"{name} must be a bounded absolute path")
        if values["tts_url"] != "http://127.0.0.1:8010/v1/audio/speech":
            raise RuntimeError(
                "ALARM_TTS_URL must use the fixed loopback Audio8 endpoint"
            )
        if not values["tts_model"] or len(values["tts_model"]) > 100:
            raise RuntimeError("ALARM_TTS_MODEL must contain 1-100 characters")
        return cls(**values)


class AlarmStore:
    def __init__(self, database_path: str, timezone_name: str) -> None:
        self.database_path = database_path
        self.timezone = ZoneInfo(timezone_name)
        self.timezone_name = timezone_name
        self._lock = threading.RLock()
        self._initialize()

    @contextlib.contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        path = Path(self.database_path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS alarms (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('timer', 'alarm')),
                    label TEXT,
                    due_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'ringing', 'dismissed', 'cancelled', 'expired')
                    ),
                    ringing_started_at REAL,
                    next_ring_at REAL
                ) STRICT
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS alarms_active_due ON alarms(status, due_at)"
            )
        os.chmod(path, 0o600)

    def _public(self, row: sqlite3.Row) -> dict[str, Any]:
        due_at = float(row["due_at"])
        local_due = datetime.fromtimestamp(due_at, self.timezone)
        return {
            "id": row["id"],
            "kind": row["kind"],
            "label": row["label"],
            "status": row["status"],
            "due_at": utc_timestamp(due_at),
            "local_due_at": local_due.isoformat(),
            "timezone": self.timezone_name,
        }

    def create(self, payload: dict[str, Any], now: float) -> dict[str, Any]:
        extra = set(payload) - {"kind", "duration_seconds", "due_at", "label"}
        if extra:
            raise RequestError(400, "request contains unsupported fields")
        kind = payload.get("kind")
        label = normalize_label(payload.get("label"))
        if kind == "timer":
            duration = payload.get("duration_seconds")
            if isinstance(duration, bool) or not isinstance(duration, int):
                raise RequestError(400, "duration_seconds must be an integer")
            if not 1 <= duration <= MAX_TIMER_SECONDS:
                raise RequestError(
                    400, "duration_seconds must be between 1 and 604800"
                )
            if payload.get("due_at") is not None:
                raise RequestError(400, "a timer cannot include due_at")
            due_at = now + duration
        elif kind == "alarm":
            if payload.get("duration_seconds") is not None:
                raise RequestError(400, "an alarm cannot include duration_seconds")
            due_at = parse_due_at(payload.get("due_at"), now)
        else:
            raise RequestError(400, "kind must be timer or alarm")

        alarm_id = secrets.token_hex(6)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO alarms(id, kind, label, due_at, created_at, status)
                VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (alarm_id, kind, label, due_at, now),
            )
            row = connection.execute(
                "SELECT * FROM alarms WHERE id = ?", (alarm_id,)
            ).fetchone()
        assert row is not None
        return self._public(row)

    def list_active(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM alarms
                WHERE status IN ('pending', 'ringing')
                ORDER BY due_at, created_at
                LIMIT ?
                """,
                (MAX_LIST_RESULTS,),
            ).fetchall()
        return [self._public(row) for row in rows]

    def cancel(self, alarm_id: str, now: float) -> dict[str, Any]:
        del now
        if not re.fullmatch(r"[0-9a-f]{12}", alarm_id):
            raise RequestError(400, "invalid alarm id")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM alarms WHERE id = ?", (alarm_id,)
            ).fetchone()
            if row is None:
                raise RequestError(404, "alarm was not found")
            if row["status"] in {"pending", "ringing"}:
                status = "dismissed" if row["status"] == "ringing" else "cancelled"
                connection.execute(
                    "UPDATE alarms SET status = ?, next_ring_at = NULL WHERE id = ?",
                    (status, alarm_id),
                )
                row = connection.execute(
                    "SELECT * FROM alarms WHERE id = ?", (alarm_id,)
                ).fetchone()
        assert row is not None
        return self._public(row)

    def dismiss_ringing(self, alarm_id: str | None) -> list[dict[str, Any]]:
        if alarm_id is not None and not re.fullmatch(r"[0-9a-f]{12}", alarm_id):
            raise RequestError(400, "invalid alarm id")
        with self._lock, self._connect() as connection:
            if alarm_id is None:
                rows = connection.execute(
                    "SELECT * FROM alarms WHERE status = 'ringing' ORDER BY due_at"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM alarms WHERE id = ? AND status = 'ringing'",
                    (alarm_id,),
                ).fetchall()
            connection.executemany(
                "UPDATE alarms SET status = 'dismissed', next_ring_at = NULL WHERE id = ?",
                [(row["id"],) for row in rows],
            )
        return [dict(self._public(row), status="dismissed") for row in rows]

    def due_to_ring(self, now: float) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE alarms
                SET status = 'expired', next_ring_at = NULL
                WHERE status = 'pending' AND due_at <= ?
                """,
                (now - MAX_RINGING_SECONDS,),
            )
            connection.execute(
                """
                UPDATE alarms
                SET status = 'ringing', ringing_started_at = ?, next_ring_at = ?
                WHERE status = 'pending' AND due_at <= ?
                """,
                (now, now, now),
            )
            connection.execute(
                """
                UPDATE alarms
                SET status = 'expired', next_ring_at = NULL
                WHERE status = 'ringing' AND ringing_started_at <= ?
                """,
                (now - MAX_RINGING_SECONDS,),
            )
            rows = connection.execute(
                """
                SELECT * FROM alarms
                WHERE status = 'ringing' AND next_ring_at <= ?
                ORDER BY due_at
                LIMIT 8
                """,
                (now,),
            ).fetchall()
            connection.executemany(
                "UPDATE alarms SET next_ring_at = ? WHERE id = ?",
                [(now + RING_INTERVAL_SECONDS, row["id"]) for row in rows],
            )
        return [self._public(row) for row in rows]


def alarm_cue() -> bytes:
    rate = 16_000
    samples: list[int] = []
    for frequency in (523.25, 659.25, 783.99):
        frames = int(rate * 0.32)
        fade = int(rate * 0.025)
        for index in range(frames):
            envelope = min(
                1.0,
                index / max(1, fade),
                (frames - index - 1) / max(1, fade),
            )
            elapsed = index / rate
            value = math.sin(2 * math.pi * frequency * elapsed)
            samples.append(round(32_767 * 0.16 * envelope * value))
        samples.extend([0] * int(rate * 0.08))
    output = io.BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(rate)
        destination.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return output.getvalue()


def validate_wav(payload: bytes) -> float:
    if not payload or len(payload) > MAX_TTS_BYTES:
        raise RuntimeError("Audio8 returned an invalid WAV size")
    try:
        with wave.open(io.BytesIO(payload), "rb") as source:
            if source.getnchannels() not in {1, 2} or source.getsampwidth() != 2:
                raise RuntimeError("Audio8 returned an unsupported WAV format")
            rate = source.getframerate()
            frames = source.getnframes()
    except (EOFError, wave.Error) as error:
        raise RuntimeError("Audio8 returned an invalid WAV") from error
    duration = frames / rate if rate > 0 else 0
    if not 0 < duration <= 30:
        raise RuntimeError("Audio8 returned an invalid WAV duration")
    return duration


def _abort_http_connection(
    connection: http.client.HTTPConnection,
    transport_socket: socket.socket | None = None,
) -> None:
    active_socket = transport_socket if transport_socket is not None else connection.sock
    if active_socket is not None:
        try:
            active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        active_socket.close()
    connection.close()


def _interrupt_http_connection(
    connection: http.client.HTTPConnection,
    transport_socket: socket.socket | None = None,
) -> None:
    """Wake a blocked HTTP operation without racing HTTPResponse.close()."""
    active_socket = transport_socket if transport_socket is not None else connection.sock
    if active_socket is not None:
        try:
            active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _read_http_body(
    response: http.client.HTTPResponse,
    connection: http.client.HTTPConnection,
    maximum_bytes: int,
    deadline: float,
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= maximum_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Audio8 response exceeded its total deadline")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        chunk = response.read1(min(65_536, maximum_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    payload = b"".join(chunks)
    if len(payload) > maximum_bytes:
        raise RuntimeError("Audio8 response was too large")
    return payload


def local_tts_request(
    url: str,
    body: bytes,
    *,
    timeout: float,
    cancel_event: threading.Event,
) -> tuple[http.client.HTTPMessage, bytes]:
    """POST to loopback Audio8 under one cancellable monotonic deadline."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Audio8 timeout must be positive")
    if cancel_event.is_set():
        raise InterruptedError("Audio8 request cancelled")
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise RuntimeError("Audio8 URL must be an explicit loopback HTTP endpoint")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=timeout)
    expired = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    transport_socket: socket.socket | None = None

    def watchdog() -> None:
        while True:
            if cancel_event.is_set():
                cancelled.set()
                _interrupt_http_connection(connection, transport_socket)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                expired.set()
                _interrupt_http_connection(connection, transport_socket)
                return
            if finished.wait(min(remaining, 0.05)):
                return

    watcher = threading.Thread(
        target=watchdog,
        name="alarm-tts-deadline",
        daemon=True,
    )
    watcher.start()
    try:
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "audio/wav",
                "Connection": "close",
            },
        )
        transport_socket = connection.sock
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("Audio8 rejected synthesis")
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as error:
                raise RuntimeError("Audio8 returned an invalid Content-Length") from error
            if not 0 <= declared_length <= MAX_TTS_BYTES:
                raise RuntimeError("Audio8 response was too large")
        payload = _read_http_body(response, connection, MAX_TTS_BYTES, deadline)
        headers = response.headers
    except (OSError, TimeoutError, http.client.HTTPException) as error:
        if cancelled.is_set():
            raise InterruptedError("Audio8 request cancelled") from error
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("Audio8 request exceeded its total deadline") from error
        raise RuntimeError("Audio8 request failed") from error
    finally:
        finished.set()
        _abort_http_connection(connection, transport_socket)
        watcher.join(timeout=0.2)
    if cancelled.is_set():
        raise InterruptedError("Audio8 request cancelled")
    if expired.is_set() or time.monotonic() > deadline:
        raise TimeoutError("Audio8 request exceeded its total deadline")
    return headers, payload


class AlarmAudio:
    def __init__(
        self,
        settings: AlarmSettings,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.settings = settings
        self.stop_event = stop_event or threading.Event()
        self.cue = alarm_cue()

    @staticmethod
    def announcement(event: dict[str, Any]) -> str:
        label = event.get("label")
        if event["kind"] == "timer":
            return f"Your {label} timer is done." if label else "Your timer is done."
        return f"Your {label} alarm is ringing." if label else "Your alarm is ringing."

    def synthesize(self, text: str) -> bytes:
        _headers, payload = local_tts_request(
            self.settings.tts_url,
            json.dumps(
                {"model": self.settings.tts_model, "input": text}
            ).encode("utf-8"),
            timeout=TTS_TIMEOUT_SECONDS,
            cancel_event=self.stop_event,
        )
        validate_wav(payload)
        return payload

    def _play_locked(self, wav_bytes: bytes) -> None:
        duration = validate_wav(wav_bytes)
        if not hasattr(os, "memfd_create"):
            raise RuntimeError("anonymous RAM playback is unavailable")
        memory_fd = os.memfd_create(
            "cerberus-alarm-wav", getattr(os, "MFD_CLOEXEC", 0)
        )
        try:
            view = memoryview(wav_bytes)
            while view:
                written = os.write(memory_fd, view)
                if written <= 0:
                    raise RuntimeError("could not stage alarm audio")
                view = view[written:]
            os.lseek(memory_fd, 0, os.SEEK_SET)
            process = subprocess.Popen(
                [
                    "/usr/bin/aplay",
                    "--quiet",
                    "--device",
                    self.settings.playback_device,
                    "--file-type",
                    "wav",
                ],
                stdin=memory_fd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            os.close(memory_fd)
        try:
            return_code = process.wait(timeout=max(10, duration + 5))
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=2)
            raise RuntimeError("alarm playback timed out") from error
        if return_code:
            raise RuntimeError(f"alarm playback failed with status {return_code}")

    def ring(self, event: dict[str, Any]) -> None:
        if self.stop_event.is_set():
            return
        lock_fd = acquire_playback_lock(self.settings.playback_lock_path, 30)
        try:
            self._play_locked(self.cue)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
        try:
            spoken = self.synthesize(self.announcement(event))
        except (OSError, RuntimeError):
            return
        if self.stop_event.is_set():
            return
        lock_fd = acquire_playback_lock(self.settings.playback_lock_path, 30)
        try:
            self._play_locked(spoken)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)


class AlarmScheduler(threading.Thread):
    def __init__(
        self,
        store: AlarmStore,
        audio: AlarmAudio,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="alarm-scheduler", daemon=True)
        self.store = store
        self.audio = audio
        self.stop_event = stop_event
        self.ready_event = threading.Event()
        self._state_lock = threading.Lock()
        self._last_scan_ok = False

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            last_scan_ok = self._last_scan_ok
        if not self.is_alive():
            state = "stopped"
        elif not self.ready_event.is_set():
            state = "error" if not last_scan_ok else "starting"
        else:
            state = "ok" if last_scan_ok else "error"
        return {"status": state, "healthy": state == "ok"}

    def _record_scan(self, succeeded: bool) -> None:
        with self._state_lock:
            self._last_scan_ok = succeeded
        if succeeded:
            self.ready_event.set()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                events = self.store.due_to_ring(time.time())
            except Exception as error:
                self._record_scan(False)
                print(
                    f"Alarm scheduler scan failed: {type(error).__name__}",
                    flush=True,
                )
                self.stop_event.wait(0.25)
                continue
            self._record_scan(True)
            for event in events:
                if self.stop_event.is_set():
                    return
                try:
                    self.audio.ring(event)
                except Exception as error:
                    print(f"Alarm ring failed: {type(error).__name__}", flush=True)
            self.stop_event.wait(0.25)


class AlarmHttpServer(ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(
        self,
        socket_path: str,
        store: AlarmStore,
        clock: Callable[[], float] = time.time,
        *,
        scheduler: AlarmScheduler | None = None,
        max_connections: int = MAX_HTTP_CONNECTIONS,
    ) -> None:
        self.store = store
        self.clock = clock
        self.scheduler = scheduler
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        path = Path(socket_path)
        if path.exists() or path.is_symlink():
            path.unlink()
        super().__init__(socket_path, AlarmRequestHandler)
        os.chmod(socket_path, 0o600)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            body = b'{"error":"server busy"}'
            try:
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Cache-Control: no-store\r\n"
                    b"Retry-After: 1\r\n"
                    b"Connection: close\r\n\r\n"
                    + body
                )
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: Any,
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()


class AlarmRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CerberusAlarms/1"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self._deadline_lock = threading.Lock()
        self._deadline_done = threading.Event()
        self._deadline_expired = threading.Event()
        self._phase_deadline: float | None = time.monotonic() + HEADER_TIMEOUT_SECONDS
        self._deadline_thread = threading.Thread(
            target=self._deadline_watchdog,
            name="alarm-api-deadline",
            daemon=True,
        )
        self._deadline_thread.start()

    def finish(self) -> None:
        self._deadline_done.set()
        try:
            super().finish()
        finally:
            self._deadline_thread.join(timeout=0.2)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
            self.close_connection = True

    def parse_request(self) -> bool:
        try:
            return super().parse_request()
        finally:
            self._clear_phase_deadline()

    def _set_phase_deadline(self, seconds: float) -> float:
        deadline = time.monotonic() + seconds
        self._deadline_expired.clear()
        with self._deadline_lock:
            self._phase_deadline = deadline
        return deadline

    def _clear_phase_deadline(self) -> None:
        with self._deadline_lock:
            self._phase_deadline = None

    def _deadline_watchdog(self) -> None:
        while not self._deadline_done.is_set():
            with self._deadline_lock:
                deadline = self._phase_deadline
            if deadline is None:
                self._deadline_done.wait(0.05)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._deadline_expired.set()
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self._deadline_done.wait(min(remaining, 0.05))

    @property
    def alarm_server(self) -> AlarmHttpServer:
        assert isinstance(self.server, AlarmHttpServer)
        return self.server

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        deadline = self._set_phase_deadline(WRITE_TIMEOUT_SECONDS)
        try:
            self.connection.settimeout(max(0.01, deadline - time.monotonic()))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self._write_body_with_deadline(body, deadline)
            self.wfile.flush()
        finally:
            self._clear_phase_deadline()
            self.close_connection = True

    def _write_body_with_deadline(self, body: bytes, deadline: float) -> None:
        view = memoryview(body)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response write exceeded its total deadline")
            self.connection.settimeout(remaining)
            chunk = view[:65_536]
            self.wfile.write(chunk)
            view = view[len(chunk) :]

    def _read_body(self, length: int) -> bytes:
        deadline = self._set_phase_deadline(BODY_TIMEOUT_SECONDS)
        chunks: list[bytes] = []
        remaining_bytes = length
        try:
            while remaining_bytes:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError("request body exceeded its total deadline")
                self.connection.settimeout(remaining_time)
                chunk = self.rfile.read1(min(65_536, remaining_bytes))
                if not chunk:
                    raise RequestError(400, "request body is truncated")
                chunks.append(chunk)
                remaining_bytes -= len(chunk)
        except RequestError:
            raise
        except (OSError, TimeoutError) as error:
            if self._deadline_expired.is_set() or time.monotonic() >= deadline:
                raise RequestError(408, "request body timed out") from error
            raise RequestError(400, "request body is truncated") from error
        finally:
            self._clear_phase_deadline()
        return b"".join(chunks)

    def _payload(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "chunked requests are not supported")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as error:
            raise RequestError(400, "Content-Length is required") from error
        if not 0 <= length <= MAX_BODY_BYTES:
            raise RequestError(413, "request body is too large")
        try:
            payload = json.loads(self._read_body(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RequestError(400, "request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise RequestError(400, "request body must be a JSON object")
        return payload

    def do_GET(self) -> None:
        try:
            if self.path == "/health":
                scheduler = self.alarm_server.scheduler
                scheduler_status = (
                    {"status": "ok", "healthy": True}
                    if scheduler is None
                    else scheduler.status()
                )
                self._write(
                    200 if scheduler_status["healthy"] else 503,
                    {
                        "status": "ok" if scheduler_status["healthy"] else "degraded",
                        "timezone": self.alarm_server.store.timezone_name,
                        "scheduler": scheduler_status["status"],
                    },
                )
            elif self.path == "/v1/alarms":
                self._write(200, {"alarms": self.alarm_server.store.list_active()})
            else:
                raise RequestError(404, "not found")
        except RequestError as error:
            self._write(error.status, {"error": str(error)})

    def do_POST(self) -> None:
        try:
            now = self.alarm_server.clock()
            if self.path == "/v1/alarms":
                alarm = self.alarm_server.store.create(self._payload(), now)
                self._write(201, {"alarm": alarm})
                return
            cancel = re.fullmatch(r"/v1/alarms/([0-9a-f]{12})/cancel", self.path)
            if cancel:
                if self._payload():
                    raise RequestError(400, "cancel body must be empty")
                alarm = self.alarm_server.store.cancel(cancel.group(1), now)
                self._write(200, {"alarm": alarm})
                return
            if self.path == "/v1/alarms/dismiss":
                payload = self._payload()
                if set(payload) - {"id"}:
                    raise RequestError(400, "request contains unsupported fields")
                alarm_id = payload.get("id")
                if alarm_id is not None and not isinstance(alarm_id, str):
                    raise RequestError(400, "id must be a string")
                dismissed = self.alarm_server.store.dismiss_ringing(alarm_id)
                self._write(200, {"dismissed": dismissed})
                return
            raise RequestError(404, "not found")
        except RequestError as error:
            self._write(error.status, {"error": str(error)})
        except Exception as error:
            print(f"Alarm API error: {type(error).__name__}", flush=True)
            self._write(500, {"error": "internal error"})


def systemd_notify(message: str) -> bool:
    """Send a best-effort readiness/status datagram without extra packages."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    notifier = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
    try:
        notifier.connect(address)
        notifier.sendall(message.encode("utf-8"))
    except OSError:
        return False
    finally:
        notifier.close()
    return True


def run(settings: AlarmSettings) -> None:
    stop_event = threading.Event()
    store = AlarmStore(settings.database_path, settings.timezone_name)
    audio = AlarmAudio(settings, stop_event)
    scheduler = AlarmScheduler(store, audio, stop_event)
    server = AlarmHttpServer(settings.socket_path, store, scheduler=scheduler)

    def stop(*_args: Any) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler.start()
    if not scheduler.ready_event.wait(SCHEDULER_READY_TIMEOUT_SECONDS):
        stop_event.set()
        scheduler.join(timeout=1)
        server.server_close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(settings.socket_path)
        raise RuntimeError("alarm scheduler did not become ready")
    systemd_notify(
        f"READY=1\nSTATUS=Alarm API and scheduler ready in {settings.timezone_name}"
    )
    print(f"Alarm service ready in {settings.timezone_name}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        systemd_notify("STOPPING=1\nSTATUS=Alarm service stopping")
        stop_event.set()
        scheduler.join(timeout=5)
        server.server_close()
        try:
            os.unlink(settings.socket_path)
        except FileNotFoundError:
            pass


def main() -> None:
    run(AlarmSettings.from_environment())


if __name__ == "__main__":
    main()
