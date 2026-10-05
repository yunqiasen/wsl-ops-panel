from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from threading import Lock
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app.agent_router.transforms import (
    UnsupportedProtocolTransform,
    SseTransformer,
    transform_request,
    transform_response,
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


class RouterHealth:
    """Small in-memory circuit state used by the optional provider fallback."""

    def __init__(self) -> None:
        self.states: dict[str, dict[str, float | int]] = {}
        self._lock = Lock()

    def available(self, key: str, now: float | None = None) -> bool:
        current = monotonic() if now is None else now
        with self._lock:
            state = self.states.get(key)
            return state is None or float(state.get("open_until", 0)) <= current

    def record(
        self,
        key: str,
        *,
        success: bool,
        threshold: int,
        cooldown_seconds: int,
        now: float | None = None,
    ) -> None:
        current = monotonic() if now is None else now
        with self._lock:
            state = self.states.setdefault(key, {"failures": 0, "open_until": 0.0})
            if success:
                state["failures"] = 0
                state["open_until"] = 0.0
                return
            failures = int(state.get("failures", 0)) + 1
            state["failures"] = failures
            if failures >= threshold:
                state["open_until"] = current + cooldown_seconds


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
    health_state = RouterHealth()

    app = FastAPI(title="WSL Agent Router", docs_url=None, redoc_url=None)
    app.state.router_store = store
    app.state.router_counters = counters
    app.state.router_health = health_state
    app.state.router_circuits = health_state.states

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
                health=health_state,
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
    health: RouterHealth | None = None,
) -> Response:
    """Try the selected provider and, when enabled, its ordered fallbacks."""
    health = health or RouterHealth()
    primary = dict(profile)
    provider_ids = config.get("provider_ids")
    if isinstance(provider_ids, dict):
        primary.setdefault("provider_id", provider_ids.get(client_id))
    auto_failover = bool(profile.get("auto_failover", False))
    # CC Switch 的故障转移关闭时只使用当前 Provider，并且不受熔断状态影响。
    # 备用链只有在显式开启故障转移后才参与请求。
    queue_ids: list[str] | None = None
    failover_queues = config.get("failover_queues")
    if isinstance(failover_queues, dict) and client_id in failover_queues:
        raw_queue = failover_queues.get(client_id)
        queue_ids = [str(item) for item in raw_queue] if isinstance(raw_queue, list) else []
    candidates = (
        _provider_candidates(primary, queue_ids) if auto_failover else [primary]
    )
    max_attempts = 1
    if auto_failover:
        try:
            max_attempts = max(1, min(len(candidates), int(profile.get("max_retries", 0)) + 1))
        except (TypeError, ValueError):
            max_attempts = 1
    threshold = _safe_router_int(profile.get("failure_threshold"), 3, 1, 100)
    cooldown = _safe_router_int(profile.get("cooldown_seconds"), 60, 0, 86400)
    attempted = 0
    last_error: Exception | None = None
    for index, candidate in enumerate(candidates):
        if attempted >= max_attempts:
            break
        provider_id = str(candidate.get("provider_id") or ("primary" if index == 0 else f"fallback-{index}"))
        key = f"{client_id}:{provider_id}"
        if auto_failover and not health.available(key):
            continue
        attempted += 1
        retryable_response = False
        try:
            response = await _proxy_request_once(
                client_id,
                route_path,
                request,
                candidate,
                config,
                counter,
                transport=transport,
                client_factory=client_factory,
                finish_counter=False,
            )
            retryable_response = _retryable_status(response.status_code)
            if retryable_response and attempted < max_attempts:
                if isinstance(response, StreamingResponse):
                    await _close_streaming_response(response)
                health.record(
                    key,
                    success=False,
                    threshold=threshold,
                    cooldown_seconds=cooldown,
                )
                continue
            # Client/request errors are neutral: they must not open a Provider
            # circuit. Retryable upstream errors do count toward cooling down.
            health.record(
                key,
                success=(response.status_code < 400 or not retryable_response),
                threshold=threshold,
                cooldown_seconds=cooldown,
            )
            # The final attempt owns completion for non-streaming responses;
            # StreamingResponse completes its counter when its iterator ends.
            if isinstance(response, StreamingResponse):
                response.body_iterator = _counter_owned_iterator(
                    response.body_iterator,
                    counter,
                    success=response.status_code < 400,
                )
            else:
                counter.finish(response.status_code < 400)
            return response
        except (httpx.HTTPError, RouterProxyError) as exc:
            last_error = exc
            health.record(
                key,
                success=False,
                threshold=threshold,
                cooldown_seconds=cooldown,
            )
            if attempted >= max_attempts:
                raise
            continue
    if last_error is not None:
        raise last_error
    raise RouterProxyError("all configured providers are cooling down")



def _provider_candidates(
    profile: dict[str, Any], provider_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    primary = dict(profile)
    primary.pop("fallbacks", None)
    result = [primary]
    seen = {str(primary.get("provider_id") or "primary")}
    fallbacks = profile.get("fallbacks")
    if isinstance(fallbacks, list):
        for item in fallbacks:
            if not isinstance(item, dict):
                continue
            candidate = dict(item)
            provider_id = str(candidate.get("provider_id") or "").strip()
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            result.append(candidate)
    if provider_ids is None:
        return result
    by_id = {str(item.get("provider_id") or "primary"): item for item in result}
    return [by_id[provider_id] for provider_id in provider_ids if provider_id in by_id]


def _safe_router_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def _retryable_status(status_code: int) -> bool:
    # Match CC Switch's provider error buckets: request-shape errors are
    # client-side and neutral; auth/quota/not-found and 5xx may be solved by
    # another Provider or route.
    non_retryable = {400, 405, 406, 413, 414, 415, 422, 501}
    return status_code >= 400 and status_code not in non_retryable


async def _counter_owned_iterator(
    iterator: Any,
    counter: CounterHandle,
    *,
    success: bool,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in iterator:
            yield chunk
    except BaseException:
        # Cancellation/GeneratorExit are terminal request failures too; using
        # BaseException prevents an active counter from leaking on disconnect.
        counter.finish(False)
        raise
    else:
        counter.finish(success)


async def _close_streaming_response(response: StreamingResponse) -> None:
    """Close both the response iterator and the underlying upstream client."""
    iterator = getattr(response, "body_iterator", None)
    close_iterator = getattr(iterator, "aclose", None)
    try:
        if close_iterator is not None:
            await close_iterator()
    finally:
        close_upstream = getattr(response, "_agent_router_close", None)
        if close_upstream is not None:
            await close_upstream()


async def _proxy_request_once(
    client_id: str,
    route_path: str,
    request: Request,
    profile: dict[str, Any],
    config: dict[str, Any],
    counter: CounterHandle,
    *,
    transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None,
    client_factory: Callable[..., httpx.AsyncClient] | None,
    finish_counter: bool = True,
) -> Response:
    body_bytes = await request.body()
    body = _decode_json(body_bytes)
    source_format = _detect_request_format(client_id, route_path)
    target_format = str(profile.get("api_format") or source_format)
    transformed_body: dict[str, Any] = body
    endpoint = "/" + route_path.lstrip("/")
    if endpoint.rstrip("/") == "/v1":
        endpoint = {
            "anthropic": "/v1/messages",
            "openai_chat": "/v1/chat/completions",
            "openai_responses": "/v1/responses",
            "gemini": "/v1beta/models",
        }[source_format]
    if body and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        request_body = dict(body)
        if source_format == "gemini" and not request_body.get("model"):
            model = _gemini_model_from_path(route_path)
            if model:
                request_body["model"] = model
        transformed = transform_request(source_format, target_format, request_body)
        transformed_body = transformed.body
        if source_format != target_format:
            endpoint = transformed.endpoint
    _apply_model_mapping(transformed_body, profile)
    url = _upstream_url(
        str(profile.get("base_url") or ""),
        endpoint,
        full_url=bool(profile.get("full_url", False)),
    )
    headers, params = _upstream_auth(profile, request)
    url = _merge_query_params(url, params)
    params = None
    lowered_route = route_path.lower()
    is_stream = (
        bool(transformed_body.get("stream"))
        or "text/event-stream" in request.headers.get("accept", "")
        or "streamgeneratecontent" in lowered_route
        or ":streamgeneratecontent" in url.lower()
    )
    timeout = httpx.Timeout(120.0, connect=15.0)
    client_options: dict[str, Any] = {"timeout": timeout, "follow_redirects": True}
    if transport is not None:
        client_options["transport"] = transport
    outbound_proxy = config.get("outbound_proxy")
    if outbound_proxy and bool(profile.get("use_outbound_proxy", True)) and transport is None:
        client_options["proxy"] = str(outbound_proxy)
    factory = client_factory or httpx.AsyncClient
    upstream = factory(**client_options)
    if is_stream:
        try:
            response = await upstream.send(
                upstream.build_request(
                    request.method,
                    url,
                    headers=headers,
                    params=params,
                    json=transformed_body if body else None,
                    content=None if body else body_bytes,
                ),
                stream=True,
            )
        except BaseException:
            await upstream.aclose()
            if finish_counter:
                counter.finish(False)
            raise
        status_code = response.status_code
        response_headers = _safe_response_headers(response.headers)
        iterator = response.aiter_bytes()
        try:
            first_chunk = await anext(iterator)
        except StopAsyncIteration:
            first_chunk = b""
        except Exception:
            await response.aclose()
            await upstream.aclose()
            if finish_counter:
                counter.finish(False)
            raise
        transformer = (
            SseTransformer(target_format, source_format)
            if source_format != target_format
            else None
        )
        closed = False

        async def close_upstream() -> None:
            nonlocal closed
            if closed:
                return
            closed = True
            await response.aclose()
            await upstream.aclose()

        async def stream_body() -> AsyncIterator[bytes]:
            try:
                if first_chunk:
                    if transformer is None:
                        yield first_chunk
                    else:
                        for output in transformer.feed(first_chunk):
                            yield output
                async for raw_chunk in iterator:
                    if transformer is None:
                        yield raw_chunk
                    else:
                        for output in transformer.feed(raw_chunk):
                            yield output
                if transformer is not None:
                    for output in transformer.finish():
                        yield output
                if finish_counter:
                    counter.finish(status_code < 400)
            except BaseException:
                if finish_counter:
                    counter.finish(False)
                raise
            finally:
                await close_upstream()

        streamed = StreamingResponse(
            stream_body(),
            status_code=status_code,
            media_type="text/event-stream",
            headers=response_headers,
        )
        # `_proxy_request` uses this when a retryable response is discarded
        # before Starlette starts iterating it.
        streamed._agent_router_close = close_upstream  # type: ignore[attr-defined]
        return streamed

    try:
        response = await upstream.request(
            request.method,
            url,
            headers=headers,
            params=params,
            json=transformed_body if body else None,
            content=None if body else body_bytes,
        )
        status_code = response.status_code
        raw_body = response.content
        response_headers = _safe_response_headers(response.headers)
    finally:
        await upstream.aclose()
    if finish_counter:
        counter.finish(status_code < 400)
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



def _gemini_model_from_path(route_path: str) -> str | None:
    lowered = route_path.lower()
    marker = "/models/"
    index = lowered.find(marker)
    if index < 0:
        return None
    value = route_path[index + len(marker) :]
    return value.split(":", 1)[0].split("/", 1)[0] or None

def _detect_request_format(client_id: str, route_path: str) -> str:
    lowered = route_path.lower()
    if client_id == "gemini" or "generatecontent" in lowered or "streamgeneratecontent" in lowered:
        return "gemini"
    if "/messages" in lowered or client_id in {"claude", "claude-desktop"} and "chat" not in lowered:
        return "anthropic"
    if "/responses" in lowered or (
        client_id in {"codex", "grokbuild"} and "/chat/" not in lowered
    ):
        return "openai_responses"
    return "openai_chat"


def _upstream_url(base_url: str, endpoint: str, *, full_url: bool = False) -> str:
    if not base_url:
        raise RouterProxyError("provider base URL is empty")
    base = base_url.strip()
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RouterProxyError("provider base URL is invalid")
    endpoint_parsed = urlsplit(endpoint if endpoint.startswith("/") else f"/{endpoint}")
    if full_url:
        path = parsed.path or "/"
        query = parsed.query
    else:
        base_path = parsed.path.rstrip("/")
        endpoint_path = "/" + endpoint_parsed.path.lstrip("/")
        if base_path and endpoint_path.startswith(base_path + "/"):
            path = endpoint_path
        elif base_path.endswith("/v1") and endpoint_path.startswith("/v1/"):
            path = base_path + endpoint_path[3:]
        elif base_path.endswith("/v1beta") and endpoint_path.startswith("/v1beta/"):
            path = base_path + endpoint_path[7:]
        else:
            path = base_path + endpoint_path
        query = "&".join(
            item for item in (parsed.query, endpoint_parsed.query) if item
        )
    return urlunsplit((parsed.scheme, parsed.netloc, path or "/", query, ""))


def _merge_query_params(url: str, params: list[tuple[str, str]]) -> str:
    if not params:
        return url
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend((str(key), str(value)) for key, value in params)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


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
