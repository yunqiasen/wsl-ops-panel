from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.agent_router.transforms import (
    UnsupportedProtocolTransform,
    transform_request,
    transform_response,
    transform_sse,
)
from app.services.agent_router_config import AgentRouterConfigStore
from app.services.agent_providers import REDACTED_SECRET


@dataclass
class RouterCounters:
    active: int = 0
    total: int = 0
    success: int = 0
    failure: int = 0
    _lock: Any = field(default_factory=Lock, repr=False)

    def start(self) -> "CounterHandle":
        with self._lock:
            self.active += 1
            self.total += 1
        return CounterHandle(self)

    def finish(self, success: bool) -> None:
        with self._lock:
            self.active = max(0, self.active - 1)
            if success:
                self.success += 1
            else:
                self.failure += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "active": self.active,
                "total": self.total,
                "success": self.success,
                "failure": self.failure,
            }


@dataclass
class CounterHandle:
    counters: RouterCounters
    finished: bool = False

    def finish(self, success: bool) -> None:
        if self.finished:
            return
        self.finished = True
        self.counters.finish(success)


class RouterProxyError(RuntimeError):
    pass


def create_agent_router_app(
    *,
    store: AgentRouterConfigStore | None = None,
    transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    client_factory: Callable[..., httpx.AsyncClient] | None = None,
) -> FastAPI:
    if store is None:
        from os import environ
        from pathlib import Path

        config_path = Path(environ.get("AGENT_ROUTER_CONFIG", "data/agent/router.json"))
        store = AgentRouterConfigStore(config_path.parent)
    counters = RouterCounters()

    app = FastAPI(title="WSL Agent Router", docs_url=None, redoc_url=None)
    app.state.router_store = store
    app.state.router_counters = counters

    @app.get("/health")
    async def health() -> dict[str, Any]:
        config = store.snapshot()
        return {
            "status": "ok",
            "listen_address": config["listen_address"],
            "listen_port": config["listen_port"],
            "counters": counters.snapshot(),
        }

    @app.get("/status")
    async def status() -> dict[str, Any]:
        return {"config": store.public_snapshot(), "counters": counters.snapshot()}

    @app.api_route(
        "/{client_id}/{route_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy(client_id: str, route_path: str, request: Request) -> Response:
        client = _clean_client_id(client_id)
        profile = store.get_provider(client)
        if profile is None:
            return JSONResponse(
                {"error": {"type": "router_provider_missing", "message": "provider is not configured"}},
                status_code=503,
            )
        counter = counters.start()
        try:
            return await _proxy_request(
                client,
                route_path,
                request,
                profile,
                store.snapshot(),
                counter,
                transport=transport,
                client_factory=client_factory,
            )
        except UnsupportedProtocolTransform as exc:
            counter.finish(False)
            return JSONResponse(
                {"error": {"type": "unsupported_protocol", "message": str(exc)}},
                status_code=502,
            )
        except (httpx.HTTPError, RouterProxyError):
            counter.finish(False)
            # Never include a URL, request body, or exception string: those can
            # contain credentials supplied by a custom provider.
            return JSONResponse(
                {"error": {"type": "upstream_error", "message": "upstream request failed"}},
                status_code=502,
            )
        except Exception:
            counter.finish(False)
            return JSONResponse(
                {"error": {"type": "router_error", "message": "router request failed"}},
                status_code=500,
            )

    return app


async def _proxy_request(
    client_id: str,
    route_path: str,
    request: Request,
    profile: dict[str, Any],
    config: dict[str, Any],
    counter: CounterHandle,
    *,
    transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None,
    client_factory: Callable[..., httpx.AsyncClient] | None,
) -> Response:
    body_bytes = await request.body()
    body = _decode_json(body_bytes)
    source_format = _detect_request_format(client_id, route_path)
    target_format = str(profile.get("api_format") or source_format)
    transformed_body: dict[str, Any] = body
    endpoint = "/" + route_path.lstrip("/")
    if body and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        transformed = transform_request(source_format, target_format, body)
        transformed_body = transformed.body
        if source_format != target_format:
            endpoint = transformed.endpoint
    _apply_model_mapping(transformed_body, profile)
    url = _upstream_url(str(profile.get("base_url") or ""), endpoint)
    headers, params = _upstream_auth(profile, request)
    is_stream = bool(transformed_body.get("stream")) or "text/event-stream" in request.headers.get("accept", "")
    timeout = httpx.Timeout(120.0, connect=15.0)
    client_options: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if transport is not None:
        client_options["transport"] = transport
    outbound_proxy = config.get("outbound_proxy")
    if outbound_proxy and bool(profile.get("use_outbound_proxy", True)) and transport is None:
        client_options["proxy"] = str(outbound_proxy)
    factory = client_factory or httpx.AsyncClient
    async with factory(**client_options) as upstream:
        if is_stream:
            async with upstream.stream(
                request.method,
                url,
                headers=headers,
                params=params,
                json=transformed_body if body else None,
                content=None if body else body_bytes,
            ) as response:
                raw_chunks = [chunk async for chunk in response.aiter_bytes()]
                status_code = response.status_code
                response_headers = _safe_response_headers(response.headers)
        else:
            response = await upstream.request(
                request.method,
                url,
                headers=headers,
                params=params,
                json=transformed_body if body else None,
                content=None if body else body_bytes,
            )
            status_code = response.status_code
            raw_chunks = [response.content]
            response_headers = _safe_response_headers(response.headers)
    counter.finish(status_code < 400)
    if is_stream:
        if source_format != target_format:
            raw_chunks = transform_sse(target_format, source_format, raw_chunks)
        return StreamingResponse(
            _yield_chunks(raw_chunks),
            status_code=status_code,
            media_type="text/event-stream",
            headers=response_headers,
        )
    raw_body = raw_chunks[0] if raw_chunks else b""
    content_type = response_headers.get("content-type", "")
    if source_format != target_format and _is_json_content(content_type, raw_body):
        decoded = _decode_json(raw_body)
        if decoded:
            decoded = transform_response(target_format, source_format, decoded)
            raw_body = json.dumps(decoded, ensure_ascii=False).encode("utf-8")
            response_headers["content-type"] = "application/json"
    return Response(
        content=raw_body,
        status_code=status_code,
        headers=response_headers,
        media_type=None,
    )


async def _yield_chunks(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _decode_json(value: bytes) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _detect_request_format(client_id: str, route_path: str) -> str:
    lowered = route_path.lower()
    if client_id == "gemini" or "generatecontent" in lowered or "streamgeneratecontent" in lowered:
        return "gemini"
    if "/messages" in lowered or client_id in {"claude", "claude-desktop"} and "chat" not in lowered:
        return "anthropic"
    if "/responses" in lowered or client_id == "codex" and "/chat/" not in lowered:
        return "openai_responses"
    return "openai_chat"


def _upstream_url(base_url: str, endpoint: str) -> str:
    if not base_url:
        raise RouterProxyError("provider base URL is empty")
    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RouterProxyError("provider base URL is invalid")
    base_path = parsed.path.rstrip("/")
    endpoint_path = "/" + endpoint.lstrip("/")
    if base_path and endpoint_path.startswith(base_path + "/"):
        path = endpoint_path
    elif base_path.endswith("/v1") and endpoint_path.startswith("/v1/"):
        path = base_path + endpoint_path[3:]
    elif base_path.endswith("/v1beta") and endpoint_path.startswith("/v1beta/"):
        path = base_path + endpoint_path[7:]
    else:
        path = base_path + endpoint_path
    return urlunsplit((parsed.scheme, parsed.netloc, path or "/", "", ""))


def _upstream_auth(
    profile: dict[str, Any], request: Request
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    raw = profile.get("headers")
    headers = (
        {str(key): str(value) for key, value in raw.items()}
        if isinstance(raw, dict)
        else {}
    )
    # Keep only safe request headers; never forward the client's credential or
    # host to a different upstream.
    for key in ("content-type", "accept", "user-agent"):
        if key in request.headers and key not in headers:
            headers[key] = request.headers[key]
    params = [(str(key), str(value)) for key, value in request.query_params.multi_items()]
    api_key = str(profile.get("api_key") or "")
    auth_mode = str(profile.get("auth_mode") or "none")
    if api_key and auth_mode == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    elif api_key and auth_mode == "x-api-key":
        headers["x-api-key"] = api_key
    elif api_key and auth_mode == "query":
        params.append(("key", api_key))
    if profile.get("api_format") == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    return headers, params


def _apply_model_mapping(body: dict[str, Any], profile: dict[str, Any]) -> None:
    current = body.get("model")
    mapping = profile.get("model_map")
    if isinstance(mapping, dict) and current in mapping:
        body["model"] = mapping[current]
    elif profile.get("model"):
        body["model"] = profile["model"]


def _is_json_content(content_type: str, body: bytes) -> bool:
    return "json" in content_type.lower() or body.lstrip().startswith((b"{", b"["))


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "content-type",
        "cache-control",
        "etag",
        "last-modified",
        "retry-after",
        "x-request-id",
    }
    return {
        key: value
        for key, value in headers.items()
        if key.lower() in allowed and REDACTED_SECRET not in value
    }


def _clean_client_id(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum() or ch in "-_.")
