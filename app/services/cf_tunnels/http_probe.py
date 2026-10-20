"""Bounded lightweight reads; redirects stay on the already-owned HTTP origin."""

import asyncio
import hashlib
import re
import socket
import ssl

import httpx

from app.services.cf_tunnels.models import CFError


def http_error_message(error: httpx.HTTPError) -> str:
    # Transport adapters retain the OS/TLS cause. Read its type, never report
    # its text: that text can contain the request URL or credentials.
    if isinstance(error, httpx.TimeoutException):
        return "HTTP 网络访问超时"
    pending = [error]
    seen = set()
    while pending:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            return "HTTPS 证书验证或 TLS 握手失败"
        if isinstance(cause, socket.gaierror):
            return "DNS 解析失败"
        parent = cause.__cause__ or cause.__context__
        if parent is not None:
            pending.append(parent)
        if isinstance(cause, BaseExceptionGroup):
            pending.extend(cause.exceptions)
    if isinstance(error, httpx.ConnectError):
        # Some transports expose only a message. Accept specific resolver/TLS
        # diagnostics, after excluding URLs; a hostname/path is not a layer.
        detail = re.sub(r"\b[a-z][a-z0-9+.-]*://\S+", "", str(error).lower())
        if any(word in detail for word in (
            "certificate verify failed", "certificate verification failed",
            "tls handshake failed", "ssl handshake failed",
        )):
            return "HTTPS 证书验证或 TLS 握手失败"
        if any(word in detail for word in (
            "name or service not known", "name resolution",
            "nodename nor servname", "getaddrinfo failed",
        )):
            return "DNS 解析失败"
        return "TCP 连接失败"
    if isinstance(error, httpx.RemoteProtocolError):
        return "HTTP 协议响应无效"
    return f"HTTP 网络访问失败 ({type(error).__name__})"


def _interstitial_reason(headers: httpx.Headers, body: bytes) -> str | None:
    content = body.lower()
    if (
        headers.get("cf-mitigated", "").lower() == "challenge"
        or b"_cf_chl_opt" in content
        or b"/cdn-cgi/challenge-platform/" in content
    ):
        return "Cloudflare 验证页面，尚未到达项目业务页面"
    if (
        b"cf-error-details" in content
        or b"cf-error-code" in content
        or b"cloudflare tunnel error" in content
    ):
        return "Cloudflare 错误页面，项目入口尚未连通"
    return None


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
                        interstitial = _interstitial_reason(
                            response.headers, bytes(body)
                        )
                        login = response.status_code in {401, 403} and not interstitial
                        reachable = not interstitial and (
                            200 <= response.status_code < 300 or login
                        )
                        return {
                            "status_code": response.status_code,
                            "reachable": reachable,
                            "requires_login": login,
                            "reason": interstitial
                            or (
                                "可达，需要登录"
                                if login
                                else (
                                    None
                                    if reachable
                                    else f"HTTP {response.status_code}"
                                )
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
