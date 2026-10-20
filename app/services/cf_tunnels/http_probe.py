"""Bound a lightweight read by wall time, not just socket inactivity."""

import asyncio
import hashlib

import httpx

from app.services.cf_tunnels.models import CFError


def probe(
    transport: httpx.AsyncBaseTransport | None,
    url: str,
    *,
    timeout: float = 5,
    stage: str = "origin_probe",
) -> dict[str, object]:
    async def read():
        async with asyncio.timeout(timeout):
            async with httpx.AsyncClient(
                transport=transport, trust_env=False
            ) as client:
                async with client.stream(
                    "GET",
                    url,
                    timeout=timeout,
                    follow_redirects=False,
                    headers={"Accept-Encoding": "identity"},
                ) as response:
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part[: 65536 - len(body)])
                        if len(body) >= 65536:
                            break
                    content_type = response.headers.get("content-type", "").split(";")[
                        0
                    ]
                    return {
                        "status_code": response.status_code,
                        "reachable": 200 <= response.status_code < 300,
                        "fingerprint": hashlib.sha256(
                            content_type.encode() + b"\0" + body
                        ).hexdigest(),
                    }

    try:
        return asyncio.run(read())
    except TimeoutError as exc:
        raise CFError(stage, "HTTP 入口核验超时") from exc
