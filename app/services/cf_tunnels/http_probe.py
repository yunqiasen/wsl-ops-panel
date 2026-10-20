"""Bounded lightweight reads; redirects stay on the already-owned HTTP origin."""

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
                current = original = httpx.URL(url)
                for redirect in range(4):
                    async with client.stream(
                        "GET",
                        current,
                        timeout=timeout,
                        follow_redirects=False,
                        headers={"Accept-Encoding": "identity"},
                    ) as response:
                        if response.is_redirect:
                            destination = current.join(
                                response.headers.get("location", "")
                            )
                            if (
                                redirect == 3
                                or destination.userinfo
                                or (
                                    destination.scheme,
                                    destination.host,
                                    destination.port,
                                )
                                != (original.scheme, original.host, original.port)
                                or not response.headers.get("location")
                            ):
                                raise CFError(stage, "入口跳转超限或离开当前服务")
                            current = destination
                            continue
                        body = bytearray()
                        async for part in response.aiter_bytes():
                            body.extend(part[: 65536 - len(body)])
                            if len(body) >= 65536:
                                break
                        content_type = response.headers.get("content-type", "").split(
                            ";"
                        )[0]
                        login = response.status_code in {401, 403}
                        reachable = 200 <= response.status_code < 300 or login
                        return {
                            "status_code": response.status_code,
                            "reachable": reachable,
                            "requires_login": login,
                            "reason": "可达，需要登录"
                            if login
                            else (
                                None if reachable else f"HTTP {response.status_code}"
                            ),
                            "fingerprint": hashlib.sha256(
                                f"{response.status_code}:{content_type}".encode()
                                + b"\0"
                                + body
                            ).hexdigest(),
                        }

    try:
        return asyncio.run(read())
    except TimeoutError as exc:
        raise CFError(stage, "HTTP 入口核验超时") from exc
