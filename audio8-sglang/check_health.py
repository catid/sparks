#!/usr/bin/env python3
"""Validate the production Audio8 backend or public gateway health contract."""

from __future__ import annotations

import http.client
import json
import sys
import urllib.parse

from gateway import validate_backend_attestation


BACKEND_URL = "http://127.0.0.1:8010/health"
GATEWAY_URL = "http://127.0.0.1:8010/health"


def get_json(url: str, timeout: float = 4.0) -> dict[str, object]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname is None or parsed.port is None:
        raise RuntimeError("health URL is invalid")
    connection = http.client.HTTPConnection(
        parsed.hostname, parsed.port, timeout=timeout
    )
    try:
        connection.request(
            "GET",
            parsed.path,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        body = response.read(65_537)
        if response.status != 200 or len(body) > 65_536:
            raise RuntimeError("health endpoint is not ready")
        if response.headers.get_content_type() != "application/json":
            raise RuntimeError("health endpoint returned the wrong content type")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise RuntimeError("health response is not an object")
        return payload
    finally:
        connection.close()


def require_optimized(attestation: dict[str, object]) -> None:
    if (
        attestation.get("cuda_graph_requested") is not True
        or attestation.get("cuda_graph_active") is not True
        or attestation.get("cuda_graph_batches") != [1, 2]
        or attestation.get("torch_compile_requested") is not True
        or attestation.get("torch_compile_active") is not True
        or attestation.get("torch_compile_batches") != [1, 2]
    ):
        raise RuntimeError("required production optimizations are not active")


def check_backend(url: str = BACKEND_URL) -> None:
    payload = get_json(url)
    if payload.get("status") != "healthy" or payload.get("running") is not True:
        raise RuntimeError("backend is not healthy")
    attestation = validate_backend_attestation(payload.get("audio8_runtime"))
    require_optimized(attestation)


def check_gateway(url: str = GATEWAY_URL) -> None:
    payload = get_json(url)
    if (
        payload.get("status") != "ok"
        or payload.get("deployment") != "production"
        or payload.get("reference_conditioning_cached") is not True
        or payload.get("sdpa_backend") != "flashinfer"
        or payload.get("fast_attention_backend") != "flashinfer"
        or payload.get("cuda_graph_active") is not True
        or payload.get("cuda_graph_batches") != [1, 2]
        or payload.get("codebook_compile_active") is not True
        or payload.get("codebook_compile_batches") != [1, 2]
    ):
        raise RuntimeError("gateway health contract is not production-ready")


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in {"backend", "gateway"}:
        raise SystemExit("usage: check_health.py backend|gateway")
    try:
        if sys.argv[1] == "backend":
            check_backend()
        else:
            check_gateway()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Audio8 health check failed: {error}") from error


if __name__ == "__main__":
    main()
