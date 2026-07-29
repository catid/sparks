#!/usr/bin/env python3
"""Thin streaming proxy that makes /v1/models survive a replica reload."""

from __future__ import annotations

import asyncio
import os
from typing import Iterable

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web


UPSTREAM = os.environ.get("ROUTER_UPSTREAM", "http://127.0.0.1:8081").rstrip("/")
MODEL_WORKERS = tuple(
    value.strip().rstrip("/")
    for value in os.environ.get(
        "ROUTER_MODEL_WORKERS",
        "http://192.168.100.10:8000,http://192.168.100.11:8000",
    ).split(",")
    if value.strip()
)
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def filtered_headers(items: Iterable[tuple[str, str]]) -> dict[str, str]:
    return {
        key: value
        for key, value in items
        if key.lower() not in HOP_BY_HOP and key.lower() != "host"
    }


async def models(request: web.Request) -> web.StreamResponse:
    session: ClientSession = request.app["client"]
    errors = []
    for worker in MODEL_WORKERS:
        try:
            async with session.get(
                f"{worker}/v1/models",
                headers=filtered_headers(request.headers.items()),
                timeout=ClientTimeout(total=5),
            ) as response:
                body = await response.read()
                if response.status == 200:
                    return web.Response(
                        body=body,
                        status=200,
                        headers=filtered_headers(response.headers.items()),
                    )
                errors.append(f"{worker}: HTTP {response.status}")
        except Exception as exc:
            errors.append(f"{worker}: {type(exc).__name__}")
    return web.json_response(
        {
            "error": {
                "message": "No Laguna replica is ready for model discovery",
                "type": "service_unavailable",
                "details": errors,
            }
        },
        status=503,
    )


async def proxy(request: web.Request) -> web.StreamResponse:
    if request.method == "GET" and request.path == "/v1/models":
        return await models(request)

    session: ClientSession = request.app["client"]
    url = f"{UPSTREAM}{request.rel_url}"
    try:
        upstream = await session.request(
            request.method,
            url,
            headers=filtered_headers(request.headers.items()),
            data=request.content.iter_chunked(1024 * 1024),
            allow_redirects=False,
        )
    except Exception as exc:
        return web.json_response(
            {
                "error": {
                    "message": f"Router upstream unavailable: {type(exc).__name__}",
                    "type": "service_unavailable",
                }
            },
            status=503,
        )

    response = web.StreamResponse(
        status=upstream.status,
        reason=upstream.reason,
        headers=filtered_headers(upstream.headers.items()),
    )
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(1024 * 1024):
            await response.write(chunk)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        upstream.release()
    await response.write_eof()
    return response


async def client_context(app: web.Application):
    timeout = ClientTimeout(total=None, connect=10, sock_connect=10, sock_read=None)
    async with ClientSession(
        timeout=timeout,
        connector=TCPConnector(limit=256),
        auto_decompress=False,
    ) as session:
        app["client"] = session
        yield


def main() -> None:
    app = web.Application(
        client_max_size=512 * 1024 * 1024,
        middlewares=[],
    )
    app.cleanup_ctx.append(client_context)
    app.router.add_route("*", "/{tail:.*}", proxy)
    web.run_app(
        app,
        host=os.environ.get("ROUTER_FRONT_HOST", "0.0.0.0"),
        port=int(os.environ.get("ROUTER_FRONT_PORT", "8080")),
        access_log=None,
    )


if __name__ == "__main__":
    main()
