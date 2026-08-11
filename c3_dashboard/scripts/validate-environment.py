#!/usr/bin/env python3
"""Validate the root-owned C3 collector/kiosk systemd environment file."""

from __future__ import annotations

import pathlib
import re
import sys
from urllib.parse import SplitResult, urlsplit


NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
OUTPUT = re.compile(r"^[A-Za-z0-9_.:-]+$")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
EXPECTED_NODES = ("cerebrus1", "cerebrus2", "cerebrus3")
REQUIRED = frozenset(
    {
        "C3_DASHBOARD_HOST",
        "C3_DASHBOARD_PORT",
        "C3_DASHBOARD_INTERVAL",
        "C3_DASHBOARD_HISTORY_POINTS",
        "C3_DASHBOARD_ALLOW_REMOTE",
        "C3_DASHBOARD_NODES",
        "C3_DASHBOARD_VLLM_METRICS_URL",
        "C3_DASHBOARD_SSH_KEY",
        "C3_DASHBOARD_SSH_KNOWN_HOSTS",
        "C3_KIOSK_URL",
        "C3_KIOSK_OUTPUT",
        "C3_KIOSK_MODE",
        "C3_KIOSK_RETRY_SECONDS",
        "C3_KIOSK_OUTPUT_WAIT_SECONDS",
    }
)


def fail(message: str) -> None:
    raise ValueError(message)


def parse_environment(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            fail(f"{path}:{number}: expected NAME=value")
        key, value = line.split("=", 1)
        if not NAME.fullmatch(key):
            fail(f"{path}:{number}: invalid environment name")
        if key in values:
            fail(f"{path}:{number}: duplicate environment name {key}")
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        if any(char in value for char in ("\x00", "\r", "\n")):
            fail(f"{path}:{number}: invalid control character")
        values[key] = value
    return values


def integer(values: dict[str, str], name: str, minimum: int, maximum: int) -> int:
    try:
        value = int(values[name])
    except ValueError as exc:
        fail(f"{name} must be an integer")
        raise AssertionError from exc
    if not minimum <= value <= maximum:
        fail(f"{name} must be between {minimum} and {maximum}")
    return value


def validated_url(value: str, name: str) -> SplitResult:
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        fail(f"{name} cannot contain credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        fail(f"{name} has an invalid port")
        raise AssertionError from exc
    if port is not None and not 1 <= port <= 65535:
        fail(f"{name} port must be between 1 and 65535")
    return parsed


def validate(values: dict[str, str]) -> None:
    missing = sorted(REQUIRED - values.keys())
    if missing:
        fail("dashboard environment is missing: " + ", ".join(missing))

    forbidden = sorted({"C3_KIOSK_DISPLAY", "C3_KIOSK_VT"} & values.keys())
    if forbidden:
        fail(
            "X display :0 and VT 7 are fixed by the unit; remove: "
            + ", ".join(forbidden)
        )
    unexpected = sorted(values.keys() - REQUIRED)
    if unexpected:
        fail("unsupported dashboard environment names: " + ", ".join(unexpected))

    if values["C3_DASHBOARD_HOST"] not in LOOPBACK_HOSTS:
        fail("C3_DASHBOARD_HOST must remain loopback-only")
    if values["C3_DASHBOARD_ALLOW_REMOTE"] != "0":
        fail("the on-screen C3 dashboard must not allow remote HTTP")
    port = integer(values, "C3_DASHBOARD_PORT", 1, 65535)
    try:
        interval = float(values["C3_DASHBOARD_INTERVAL"])
    except ValueError as exc:
        fail("C3_DASHBOARD_INTERVAL must be numeric")
        raise AssertionError from exc
    if interval != 5:
        fail("C3_DASHBOARD_INTERVAL must be 5 seconds")
    integer(values, "C3_DASHBOARD_HISTORY_POINTS", 2, 17_280)

    nodes = tuple(node.strip() for node in values["C3_DASHBOARD_NODES"].split(","))
    if nodes != EXPECTED_NODES:
        fail("C3_DASHBOARD_NODES must be cerebrus1,cerebrus2,cerebrus3")

    metrics = validated_url(
        values["C3_DASHBOARD_VLLM_METRICS_URL"],
        "C3_DASHBOARD_VLLM_METRICS_URL",
    )
    if (
        metrics.scheme != "http"
        or metrics.hostname not in {"cerebrus1", "spark1"}
        or (metrics.port or 80) != 8889
        or metrics.path != "/metrics"
        or metrics.query
        or metrics.fragment
    ):
        fail("C3_DASHBOARD_VLLM_METRICS_URL must be C1 HTTP port 8889 /metrics")

    for name in ("C3_DASHBOARD_SSH_KEY", "C3_DASHBOARD_SSH_KNOWN_HOSTS"):
        path = pathlib.PurePath(values[name])
        if (
            not path.is_absolute()
            or "@HOME@" in values[name]
            or any(char.isspace() for char in values[name])
        ):
            fail(f"{name} must be an expanded absolute path")

    kiosk = validated_url(values["C3_KIOSK_URL"], "C3_KIOSK_URL")
    kiosk_port = 80 if kiosk.port is None else kiosk.port
    if (
        kiosk.scheme != "http"
        or kiosk.hostname not in LOOPBACK_HOSTS
        or kiosk_port != port
        or kiosk.query
    ):
        fail("C3_KIOSK_URL must use loopback HTTP on C3_DASHBOARD_PORT")
    if values["C3_KIOSK_MODE"] != "1424x280":
        fail("C3_KIOSK_MODE must preserve the panel's native 1424x280 mode")
    output = values["C3_KIOSK_OUTPUT"]
    if output != "auto" and not OUTPUT.fullmatch(output):
        fail("C3_KIOSK_OUTPUT must be auto or a safe XRandR output name")
    integer(values, "C3_KIOSK_RETRY_SECONDS", 1, 300)
    integer(values, "C3_KIOSK_OUTPUT_WAIT_SECONDS", 1, 300)


def main(argv: list[str]) -> int:
    get_name: str | None = None
    if len(argv) == 4 and argv[1] == "--get":
        get_name, path_text = argv[2], argv[3]
    elif len(argv) == 2:
        path_text = argv[1]
    else:
        print(
            f"Usage: {argv[0]} [--get NAME] ENVIRONMENT_FILE",
            file=sys.stderr,
        )
        return 2
    path = pathlib.Path(path_text)
    try:
        if not path.is_file() or path.is_symlink():
            fail("environment must be a regular, non-symlink file")
        values = parse_environment(path)
        validate(values)
        if get_name is not None:
            if get_name not in values:
                fail(f"environment does not contain {get_name}")
            print(values[get_name])
    except (OSError, ValueError) as exc:
        print(f"C3 dashboard environment: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
