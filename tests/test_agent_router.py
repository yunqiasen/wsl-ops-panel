from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent_router.transforms import (
    UnsupportedProtocolTransform,
    transform_request,
    transform_response,
    transform_sse,
)
from app.services.agent_router_config import AgentRouterConfigStore


def test_router_store_is_atomic_private_and_redacted(tmp_path: Path) -> None:
    store = AgentRouterConfigStore(tmp_path)

    saved = store.update_global(
        listen_port=7888, outbound_proxy="socks5://127.0.0.1:1080"
    )
    store.set_provider(
        "codex",
        {
            "base_url": "https://relay.example/v1",
            "api_key": "secret",
            "api_format": "openai_responses",
        },
    )

    assert store.state.path.stat().st_mode & 0o777 == 0o600
    assert store.public_snapshot()["providers"]["codex"]["api_key"] == "••••••••"
    assert saved["listen_port"] == 7888
    assert store.state.get_router_snapshot()["providers"]["codex"]["api_key"] == "secret"
    assert not store.path.exists()


def test_router_store_validates_endpoint_and_keeps_takeover_state(tmp_path: Path) -> None:
    store = AgentRouterConfigStore(tmp_path)

    store.set_takeover("codex", True)
    snapshot = store.snapshot()

    assert snapshot["listen_address"] == "127.0.0.1"
    assert snapshot["listen_port"] == 7888
    assert snapshot["takeover"] == {"codex": True}
    with pytest.raises(ValueError, match="port"):
        store.update_global(listen_port=0)
    with pytest.raises(ValueError, match="proxy"):
        store.update_global(outbound_proxy="ftp://proxy.example")


def test_anthropic_request_converts_to_openai_chat() -> None:
    result = transform_request(
        "anthropic",
        "openai_chat",
        {
            "model": "claude",
            "system": "rules",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 64,
        },
    )

    assert result.body["messages"][:2] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hello"},
    ]
    assert result.body["max_tokens"] == 64
    assert result.endpoint == "/v1/chat/completions"


def test_openai_chat_response_converts_to_anthropic() -> None:
    body = transform_response(
        "openai_chat",
        "anthropic",
        {
            "id": "x",
            "model": "relay",
            "choices": [
                {
                    "message": {"content": "hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        },
    )

    assert body["content"] == [{"type": "text", "text": "hi"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 2, "output_tokens": 1}


def test_responses_and_chat_transforms_preserve_tool_calls() -> None:
    result = transform_request(
        "openai_responses",
        "openai_chat",
        {
            "model": "gpt",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "find",
                    "parameters": {"type": "object"},
                }
            ],
            "max_output_tokens": 12,
        },
    )

    assert result.body["messages"] == [{"role": "user", "content": "hi"}]
    assert result.body["tools"][0]["function"]["name"] == "lookup"
    assert result.body["max_tokens"] == 12


def test_unsupported_protocol_transform_names_both_formats() -> None:
    with pytest.raises(UnsupportedProtocolTransform, match="gemini.*anthropic"):
        transform_request("gemini", "anthropic", {"model": "x"})


def test_cross_protocol_stream_is_transformed_to_anthropic_sse() -> None:
    events = transform_sse(
        "openai_chat",
        "anthropic",
        [
            b'data: {"id":"x","model":"m","choices":[{"delta":{"content":"hi"}}]}\n\n',
            b"data: [DONE]\n\n",
        ],
    )
    rendered = b"".join(events)

    assert b"content_block_delta" in rendered
    assert b'"text":"hi"' in rendered
    assert b"message_stop" in rendered


def test_router_forwards_codex_to_arbitrary_upstream(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://relay.example/v1",
            "api_format": "openai_responses",
            "api_key": "secret",
            "model": "relay-model",
        },
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={"id": "resp-1", "model": "relay-model", "output": [{"type": "message"}]},
        )

    app = create_agent_router_app(
        store=store, transport=httpx.MockTransport(handler)
    )
    response = TestClient(app).post(
        "/codex/v1/responses", json={"model": "client-model", "input": "hello"}
    )

    assert response.status_code == 200
    assert captured["url"] == "https://relay.example/v1/responses"
    assert captured["headers"]["authorization"] == "Bearer secret"  # type: ignore[index]
    assert captured["body"]["model"] == "relay-model"  # type: ignore[index]
    assert response.json()["model"] == "relay-model"


def test_router_health_and_missing_provider(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    app = create_agent_router_app(store=AgentRouterConfigStore(tmp_path))
    client = TestClient(app)

    assert client.get("/health").json()["status"] == "ok"
    missing = client.post("/codex/v1/responses", json={"model": "x", "input": "hi"})
    assert missing.status_code == 503


def test_router_controller_uses_exact_systemd_commands(tmp_path: Path) -> None:
    from app.services.agent_router_control import AgentRouterController

    commands: list[list[str]] = []
    controller = AgentRouterController(
        AgentRouterConfigStore(tmp_path),
        home=tmp_path / "home",
        runner=lambda command: commands.append(command) or 0,
        health_probe=lambda: {"status": "ok"},
    )

    result = controller.start()

    assert result["ok"] is True
    assert commands == [["sudo", "-n", "systemctl", "start", "wsl-agent-router.service"]]


def test_router_control_api_updates_config_and_takeover(tmp_path: Path) -> None:
    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app
    from app.services.agent_router_control import AgentRouterController

    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model_provider = "original"\n', encoding="utf-8")
    (tmp_path / "categories").mkdir()
    (tmp_path / "categories" / "agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (tmp_path / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    store = AgentRouterConfigStore(tmp_path / "data" / "agent")
    controller = AgentRouterController(
        store,
        home=home,
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    app = create_app(config_root=tmp_path, node_scanner=lambda: [])
    app.state.agent_router_controller = controller
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.put(
        "/api/agent/router/config",
        json={
            "listen_address": "127.0.0.1",
            "listen_port": 7888,
            "show_home_switch": True,
            "outbound_proxy": None,
        },
    )
    takeover = client.put(
        "/api/agent/router/apps/codex/takeover", json={"enabled": True}
    )

    assert response.status_code == 200
    assert response.json()["config"]["listen_port"] == 7888
    assert takeover.status_code == 200
    assert takeover.json()["takeover"]["codex"] is True
    assert "127.0.0.1:7888" in config.read_text(encoding="utf-8")


def test_router_stop_requires_restore_for_active_takeover(tmp_path: Path) -> None:
    from app.services.agent_router_control import ActiveTakeoverError, AgentRouterController

    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model_provider = "original"\n', encoding="utf-8")
    commands: list[list[str]] = []
    controller = AgentRouterController(
        AgentRouterConfigStore(tmp_path / "data" / "agent"),
        home=home,
        runner=lambda command: commands.append(command) or 0,
        health_probe=lambda: {"status": "ok"},
    )
    controller.enable_takeover("codex")

    with pytest.raises(ActiveTakeoverError, match="codex"):
        controller.stop()
    result = controller.stop(restore_clients=True)

    assert result["ok"] is True
    assert result["restored_clients"] == ["codex"]
    assert config.read_text(encoding="utf-8") == 'model_provider = "original"\n'
    assert commands[-1][-2:] == ["stop", "wsl-agent-router.service"]


def test_router_selects_saved_arbitrary_provider_without_exposing_secret(
    tmp_path: Path,
) -> None:
    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_control import AgentRouterController

    (tmp_path / "categories").mkdir()
    (tmp_path / "categories/agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules/node-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    (tmp_path / "rules/python-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    data_root = tmp_path / "data/agent"
    AgentProviderStore(data_root).upsert_provider(
        app_id="codex",
        provider_id="relay-main",
        name="Relay Main",
        settings={
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "api_key": "router-secret",
                "model": "gpt-relay",
            }
        },
    )
    router_store = AgentRouterConfigStore(data_root)
    app = create_app(config_root=tmp_path, node_scanner=lambda: [])
    app.state.agent_router_controller = AgentRouterController(
        router_store,
        home=tmp_path / "home",
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.put(
        "/api/agent/router/apps/codex/provider",
        json={"provider_id": "relay-main"},
    )

    assert response.status_code == 200
    assert response.json()["provider_id"] == "relay-main"
    assert "router-secret" not in response.text
    assert router_store.snapshot()["providers"]["codex"]["api_key"] == "router-secret"
    assert router_store.public_snapshot()["provider_ids"]["codex"] == "relay-main"
    current = AgentProviderStore(data_root).get_provider("codex", "relay-main")
    assert current is not None and current["is_current"] is True


def test_router_detects_grokbuild_default_as_responses_protocol(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "grokbuild",
        {
            "base_url": "https://grok-relay.example/v1",
            "api_format": "openai_responses",
            "api_key": "secret",
        },
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={"id": "resp-1", "model": "grok-model", "output": []},
        )

    app = create_agent_router_app(
        store=store, transport=httpx.MockTransport(handler)
    )
    response = TestClient(app).post(
        "/grokbuild/v1", json={"model": "client-model", "input": "hello"}
    )

    assert response.status_code == 200
    assert captured["url"] == "https://grok-relay.example/v1/responses"
    assert captured["body"]["input"] == "hello"  # type: ignore[index]


def test_gemini_request_converts_to_openai_chat() -> None:
    result = transform_request(
        "gemini",
        "openai_chat",
        {
            "model": "gemini-2.5-pro",
            "systemInstruction": {"parts": [{"text": "rules"}]},
            "contents": [
                {"role": "user", "parts": [{"text": "hello"}]},
                {"role": "model", "parts": [{"text": "hi"}]},
            ],
            "generationConfig": {"maxOutputTokens": 64, "topP": 0.8},
        },
    )

    assert result.endpoint == "/v1/chat/completions"
    assert result.body["model"] == "gemini-2.5-pro"
    assert result.body["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert result.body["max_tokens"] == 64
    assert result.body["top_p"] == 0.8


def test_openai_chat_request_converts_to_dynamic_gemini_endpoint() -> None:
    result = transform_request(
        "openai_chat",
        "gemini",
        {
            "model": "gemini-2.5-flash",
            "messages": [
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "hello"},
            ],
            "stream": True,
        },
    )

    assert result.endpoint == "/v1beta/models/gemini-2.5-flash:streamGenerateContent?alt=sse"
    assert result.body["systemInstruction"] == {"parts": [{"text": "rules"}]}
    assert result.body["contents"] == [
        {"role": "user", "parts": [{"text": "hello"}]}
    ]
    assert "model" not in result.body
    assert "stream" not in result.body


def test_gemini_response_converts_to_openai_responses() -> None:
    body = transform_response(
        "gemini",
        "openai_responses",
        {
            "responseId": "gem-1",
            "modelVersion": "gemini-2.5-pro",
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": "hello"}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 3,
                "candidatesTokenCount": 2,
                "totalTokenCount": 5,
            },
        },
    )

    assert body["id"] == "gem-1"
    assert body["object"] == "response"
    assert body["output_text"] == "hello"
    assert body["usage"] == {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }


def test_sse_transformer_waits_for_complete_event_boundary() -> None:
    from app.agent_router.transforms import SseTransformer

    transformer = SseTransformer("openai_chat", "anthropic")

    assert transformer.feed(
        b'data: {"id":"x","choices":[{"delta":{"content":"hel'
    ) == []
    rendered = b"".join(transformer.feed(b'lo"}}]}\r\n\r\n'))

    assert b"message_start" in rendered
    assert b"content_block_delta" in rendered
    assert b'"text":"hello"' in rendered


def test_router_full_url_is_used_without_appending_endpoint(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://relay.example/custom/responses?tenant=one",
            "full_url": True,
            "api_format": "openai_responses",
            "api_key": "secret",
        },
    )
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, request=request, json={"id": "resp-1", "output": []})

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    response = TestClient(app).post(
        "/codex/v1/responses?trace=yes", json={"model": "x", "input": "hello"}
    )

    assert response.status_code == 200
    assert captured["url"] == "https://relay.example/custom/responses?tenant=one&trace=yes"


def test_router_returns_stream_after_first_chunk_and_closes_after_consumption(
    tmp_path: Path,
) -> None:
    import asyncio

    from starlette.requests import Request

    from app.agent_router.app import RouterCounters, _proxy_request

    class CountingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.iterated = 0
            self.closed = False

        async def __aiter__(self):
            for chunk in (b"data: first\n\n", b"data: second\n\n"):
                self.iterated += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    stream = CountingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    receive_calls = 0

    async def receive() -> dict[str, object]:
        nonlocal receive_calls
        receive_calls += 1
        return {
            "type": "http.request",
            "body": b'{"model":"x","input":"hi","stream":true}',
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/codex/v1/responses",
            "raw_path": b"/codex/v1/responses",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 7888),
        },
        receive,
    )
    counters = RouterCounters()
    handle = counters.start()

    async def exercise() -> tuple[list[bytes], dict[str, int]]:
        response = await _proxy_request(
            "codex",
            "v1/responses",
            request,
            {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "none",
            },
            {"outbound_proxy": None},
            handle,
            transport=httpx.MockTransport(handler),
            client_factory=None,
        )
        assert stream.iterated == 1
        assert counters.snapshot()["active"] == 1
        chunks = [chunk async for chunk in response.body_iterator]
        return chunks, counters.snapshot()

    chunks, snapshot = asyncio.run(exercise())

    assert b"".join(chunks) == b"data: first\n\ndata: second\n\n"
    assert stream.closed is True
    assert snapshot == {"active": 0, "total": 1, "success": 1, "failure": 0}


def test_router_forwards_openai_chat_to_gemini_native_endpoint(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://generativelanguage.example/v1beta",
            "api_format": "gemini",
            "auth_mode": "query",
            "api_key": "secret",
        },
    )
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={
                "responseId": "gem-1",
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": "hi"}]},
                        "finishReason": "STOP",
                    }
                ],
            },
        )

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    response = TestClient(app).post(
        "/codex/v1/chat/completions",
        json={"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert captured["url"] == "https://generativelanguage.example/v1beta/models/gemini-2.5-flash:generateContent?key=secret"
    assert captured["body"] == {
        "contents": [{"role": "user", "parts": [{"text": "hello"}]}]
    }
    assert response.json()["choices"][0]["message"]["content"] == "hi"


def test_router_transforms_split_gemini_sse_to_openai_chat(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://generativelanguage.example/v1beta",
            "api_format": "gemini",
            "auth_mode": "none",
        },
    )

    class GeminiStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = b'data: {"candidates":[{"content":{"parts":[{"text":"hi"}]}}]}\n\n'
            yield event[:25]
            yield event[25:]
            yield b'data: {"candidates":[{"finishReason":"STOP"}]}\n\n'

        async def aclose(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=GeminiStream(),
        )

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    response = TestClient(app).post(
        "/codex/v1/chat/completions",
        headers={"accept": "text/event-stream"},
        json={"model": "gemini-2.5-flash", "messages": [{"role": "user", "content": "hello"}], "stream": True},
    )

    assert response.status_code == 200
    assert b'"content":"hi"' in response.content
    assert b"data: [DONE]" in response.content


def test_router_falls_back_and_cools_down_failed_provider(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://primary.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
            "headers": {"X-Route": "primary"},
            "auto_failover": True,
            "max_retries": 1,
            "failure_threshold": 1,
            "cooldown_seconds": 60,
            "fallbacks": [
                {
                    "provider_id": "backup",
                    "base_url": "https://backup.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                    "headers": {"X-Route": "backup"},
                }
            ],
        },
        provider_id="primary",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        route = request.headers.get("x-route", "")
        calls.append(route)
        if route == "primary":
            return httpx.Response(503, request=request, json={"error": "down"})
        return httpx.Response(
            200,
            request=request,
            json={"id": "backup-response", "output": []},
        )

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    first = client.post("/codex/v1/responses", json={"model": "x", "input": "hi"})
    second = client.post("/codex/v1/responses", json={"model": "x", "input": "hi"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert calls == ["primary", "backup", "backup"]
    assert app.state.router_circuits["codex:primary"]["failures"] == 1


def test_router_provider_selection_builds_ordered_fallback_chain(
    tmp_path: Path,
) -> None:
    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_control import AgentRouterController

    (tmp_path / "categories").mkdir()
    (tmp_path / "categories/agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules/node-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    (tmp_path / "rules/python-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    data_root = tmp_path / "data/agent"
    providers = AgentProviderStore(data_root)
    for provider_id, host in (("primary", "primary.example"), ("backup", "backup.example")):
        providers.upsert_provider(
            app_id="codex",
            provider_id=provider_id,
            name=provider_id.title(),
            settings={
                "routing": {
                    "base_url": f"https://{host}/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                }
            },
        )
    router_store = AgentRouterConfigStore(data_root)
    app = create_app(config_root=tmp_path, node_scanner=lambda: [])
    app.state.agent_router_controller = AgentRouterController(
        router_store,
        home=tmp_path / "home",
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.put(
        "/api/agent/router/apps/codex/provider",
        json={"provider_id": "backup"},
    )

    assert response.status_code == 200
    saved = router_store.snapshot()["providers"]["codex"]
    assert router_store.snapshot()["provider_ids"]["codex"] == "backup"
    assert saved["base_url"] == "https://backup.example/v1"
    assert [item["provider_id"] for item in saved["fallbacks"]] == ["primary"]


def test_router_failover_disabled_uses_selected_provider_even_when_circuit_is_open(
    tmp_path: Path,
) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://primary.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
            "auto_failover": False,
            "failure_threshold": 1,
            "cooldown_seconds": 60,
            "fallbacks": [
                {
                    "provider_id": "backup",
                    "base_url": "https://backup.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                }
            ],
        },
        provider_id="primary",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        return httpx.Response(200, request=request, json={"id": "ok", "output": []})

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    app.state.router_health.record(
        "codex:primary", success=False, threshold=1, cooldown_seconds=60
    )

    response = TestClient(app).post(
        "/codex/v1/responses", json={"model": "x", "input": "hi"}
    )

    assert response.status_code == 200
    assert calls == ["primary.example"]


def test_router_retryable_statuses_follow_cc_switch_provider_error_buckets() -> None:
    from app.agent_router.app import _retryable_status

    for status in (401, 403, 404, 408, 409, 425, 429, 451, 500, 503):
        assert _retryable_status(status) is True
    for status in (400, 405, 406, 413, 414, 415, 422, 501):
        assert _retryable_status(status) is False


def test_router_nonretryable_client_error_does_not_poison_provider_circuit(
    tmp_path: Path,
) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://primary.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
            "auto_failover": True,
            "max_retries": 1,
            "failure_threshold": 1,
            "cooldown_seconds": 60,
            "fallbacks": [
                {
                    "provider_id": "backup",
                    "base_url": "https://backup.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                }
            ],
        },
        provider_id="primary",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.host))
        return httpx.Response(400, request=request, json={"error": "bad request"})

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    client = TestClient(app)
    first = client.post("/codex/v1/responses", json={"model": "x", "input": "bad"})
    second = client.post("/codex/v1/responses", json={"model": "x", "input": "bad"})

    assert first.status_code == 400
    assert second.status_code == 400
    assert calls == ["primary.example", "primary.example"]
    assert app.state.router_circuits.get("codex:primary", {}).get("failures", 0) == 0


def test_router_final_streaming_error_counts_as_failure(tmp_path: Path) -> None:
    import asyncio

    from starlette.requests import Request

    from app.agent_router.app import RouterCounters, _proxy_request

    class ErrorStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"error":"down"}\n\n'

        async def aclose(self) -> None:
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=ErrorStream(),
        )

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b'{"model":"x","input":"hi","stream":true}',
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/codex/v1/responses",
            "raw_path": b"/codex/v1/responses",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 7888),
        },
        receive,
    )
    counters = RouterCounters()
    handle = counters.start()

    async def exercise() -> dict[str, int]:
        response = await _proxy_request(
            "codex",
            "v1/responses",
            request,
            {
                "base_url": "https://primary.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "none",
            },
            {"outbound_proxy": None},
            handle,
            transport=httpx.MockTransport(handler),
            client_factory=None,
        )
        _ = [chunk async for chunk in response.body_iterator]
        return counters.snapshot()

    assert asyncio.run(exercise()) == {
        "active": 0,
        "total": 1,
        "success": 0,
        "failure": 1,
    }


def test_router_cancelled_stream_finishes_counter_and_closes_upstream(
    tmp_path: Path,
) -> None:
    import asyncio

    from starlette.requests import Request

    from app.agent_router.app import RouterCounters, _proxy_request

    class TrackingStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.closed = False

        async def __aiter__(self):
            yield b"data: first\n\n"
            yield b"data: second\n\n"

        async def aclose(self) -> None:
            self.closed = True

    stream = TrackingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b'{"model":"x","input":"hi","stream":true}',
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/codex/v1/responses",
            "raw_path": b"/codex/v1/responses",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 7888),
        },
        receive,
    )
    counters = RouterCounters()
    handle = counters.start()

    async def exercise() -> dict[str, int]:
        response = await _proxy_request(
            "codex",
            "v1/responses",
            request,
            {
                "base_url": "https://primary.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "none",
            },
            {"outbound_proxy": None},
            handle,
            transport=httpx.MockTransport(handler),
            client_factory=None,
        )
        _ = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return counters.snapshot()

    snapshot = asyncio.run(exercise())
    assert stream.closed is True
    assert snapshot == {"active": 0, "total": 1, "success": 0, "failure": 1}


def test_router_closes_retryable_stream_before_using_fallback(tmp_path: Path) -> None:
    from app.agent_router.app import create_agent_router_app

    class TrackingStream(httpx.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.closed = False

        async def __aiter__(self):
            yield self.body

        async def aclose(self) -> None:
            self.closed = True

    primary = TrackingStream(b'data: {"error":"down"}\n\n')
    backup = TrackingStream(b'data: {"id":"ok"}\n\n')
    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://primary.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
            "auto_failover": True,
            "max_retries": 1,
            "failure_threshold": 1,
            "cooldown_seconds": 60,
            "fallbacks": [
                {
                    "provider_id": "backup",
                    "base_url": "https://backup.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                }
            ],
        },
        provider_id="primary",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        stream = primary if request.url.host == "primary.example" else backup
        status = 503 if request.url.host == "primary.example" else 200
        return httpx.Response(
            status,
            request=request,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    app = create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    response = TestClient(app).post(
        "/codex/v1/responses",
        headers={"accept": "text/event-stream"},
        json={"model": "x", "input": "hi", "stream": True},
    )

    assert response.status_code == 200
    assert primary.closed is True
    assert backup.closed is True


def test_router_explicit_failover_queue_controls_order_and_membership(
    tmp_path: Path,
) -> None:
    from app.agent_router.app import create_agent_router_app

    store = AgentRouterConfigStore(tmp_path)
    store.set_provider(
        "codex",
        {
            "base_url": "https://current.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
            "auto_failover": True,
            "max_retries": 2,
            "fallbacks": [
                {
                    "provider_id": "backup",
                    "base_url": "https://backup.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                },
                {
                    "provider_id": "excluded",
                    "base_url": "https://excluded.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                },
            ],
        },
        provider_id="current",
    )
    store.set_failover_queue("codex", ["backup", "current"])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = str(request.url.host)
        calls.append(host)
        status = 503 if host == "backup.example" else 200
        return httpx.Response(
            status,
            request=request,
            json={"id": host, "output": []},
        )

    response = TestClient(
        create_agent_router_app(store=store, transport=httpx.MockTransport(handler))
    ).post("/codex/v1/responses", json={"model": "x", "input": "hi"})

    assert response.status_code == 200
    assert calls == ["backup.example", "current.example"]
    assert store.snapshot()["failover_queues"]["codex"] == ["backup", "current"]


def test_router_policy_enable_seeds_current_provider_as_failover_p1(
    tmp_path: Path,
) -> None:
    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_control import AgentRouterController

    (tmp_path / "categories").mkdir()
    (tmp_path / "categories/agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules/node-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    (tmp_path / "rules/python-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    data_root = tmp_path / "data/agent"
    AgentProviderStore(data_root).upsert_provider(
        app_id="codex",
        provider_id="primary",
        name="Primary",
        settings={
            "routing": {
                "base_url": "https://primary.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "none",
            }
        },
    )
    router_store = AgentRouterConfigStore(data_root)
    app = create_app(config_root=tmp_path, node_scanner=lambda: [])
    app.state.agent_router_controller = AgentRouterController(
        router_store,
        home=tmp_path / "home",
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    assert client.put(
        "/api/agent/router/apps/codex/provider", json={"provider_id": "primary"}
    ).status_code == 200

    enabled = client.put(
        "/api/agent/router/apps/codex/policy",
        json={"auto_failover": True, "max_retries": 2},
    )

    assert enabled.status_code == 200, enabled.text
    assert router_store.snapshot()["failover_queues"]["codex"] == ["primary"]


def test_router_failover_queue_api_rebuilds_provider_catalog_in_requested_order(
    tmp_path: Path,
) -> None:
    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_control import AgentRouterController

    (tmp_path / "categories").mkdir()
    (tmp_path / "categories/agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules/node-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    (tmp_path / "rules/python-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    data_root = tmp_path / "data/agent"
    providers = AgentProviderStore(data_root)
    for provider_id in ("primary", "backup", "last"):
        providers.upsert_provider(
            app_id="codex",
            provider_id=provider_id,
            name=provider_id.title(),
            settings={
                "routing": {
                    "base_url": f"https://{provider_id}.example/v1",
                    "api_format": "openai_responses",
                    "auth_mode": "none",
                }
            },
        )
    router_store = AgentRouterConfigStore(data_root)
    app = create_app(config_root=tmp_path, node_scanner=lambda: [])
    app.state.agent_router_controller = AgentRouterController(
        router_store,
        home=tmp_path / "home",
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    assert client.put(
        "/api/agent/router/apps/codex/provider", json={"provider_id": "primary"}
    ).status_code == 200

    updated = client.put(
        "/api/agent/router/apps/codex/failover-queue",
        json={"provider_ids": ["backup", "last"]},
    )
    fetched = client.get("/api/agent/router/apps/codex/failover-queue")

    assert updated.status_code == 200, updated.text
    assert fetched.status_code == 200, fetched.text
    assert updated.json()["provider_ids"] == ["backup", "last"]
    assert [item["provider_id"] for item in fetched.json()["items"]] == [
        "backup",
        "last",
    ]
    saved = router_store.snapshot()["providers"]["codex"]
    assert {item["provider_id"] for item in saved["fallbacks"]} == {"backup", "last"}


def test_gemini_request_round_trip_preserves_media_thoughts_and_tool_results() -> None:
    native = {
        "model": "gemini-2.5-pro",
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": "看看这张图"},
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": "aW1hZ2U=",
                        }
                    },
                ],
            },
            {
                "role": "model",
                "parts": [
                    {
                        "text": "先查询天气",
                        "thought": True,
                        "thoughtSignature": "sig-thought",
                    },
                    {
                        "functionCall": {
                            "id": "call-weather",
                            "name": "weather",
                            "args": {"city": "杭州"},
                        },
                        "thoughtSignature": "sig-tool",
                    },
                ],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-weather",
                            "name": "weather",
                            "response": {"temperature": 23},
                        }
                    }
                ],
            },
        ],
    }

    chat = transform_request("gemini", "openai_chat", native).body

    assert chat["messages"][0]["content"] == [
        {"type": "text", "text": "看看这张图"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
        },
    ]
    assistant = chat["messages"][1]
    assert assistant["reasoning_content"] == "先查询天气"
    assert assistant["extra_content"]["google"]["thought_signature"] == "sig-thought"
    assert assistant["tool_calls"][0] == {
        "id": "call-weather",
        "type": "function",
        "function": {
            "name": "weather",
            "arguments": '{"city": "杭州"}',
        },
        "extra_content": {"google": {"thought_signature": "sig-tool"}},
    }
    assert chat["messages"][2] == {
        "role": "tool",
        "tool_call_id": "call-weather",
        "name": "weather",
        "content": '{"temperature": 23}',
    }

    replay = transform_request("openai_chat", "gemini", chat).body
    assert replay["contents"][0]["parts"] == native["contents"][0]["parts"]
    assert replay["contents"][1]["parts"] == native["contents"][1]["parts"]
    assert replay["contents"][2]["parts"] == native["contents"][2]["parts"]


def test_openai_chat_to_gemini_resolves_tool_response_name_and_json() -> None:
    result = transform_request(
        "openai_chat",
        "gemini",
        {
            "model": "gemini-2.5-flash",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"query":"MCP"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": '{"matches":2}',
                },
            ],
        },
    )

    assert result.body["contents"][1] == {
        "role": "user",
        "parts": [
            {
                "functionResponse": {
                    "id": "call-1",
                    "name": "lookup",
                    "response": {"matches": 2},
                }
            }
        ],
    }


def test_gemini_response_preserves_reasoning_media_tool_signature_and_usage() -> None:
    body = transform_response(
        "gemini",
        "openai_chat",
        {
            "responseId": "gem-rich",
            "modelVersion": "gemini-2.5-pro",
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "text": "先分析",
                                "thought": True,
                                "thoughtSignature": "sig-thought",
                            },
                            {"text": "结果"},
                            {
                                "inlineData": {
                                    "mimeType": "image/webp",
                                    "data": "aW1hZ2U=",
                                }
                            },
                            {
                                "functionCall": {
                                    "id": "call-7",
                                    "name": "lookup",
                                    "args": {"q": "x"},
                                },
                                "thoughtSignature": "sig-tool",
                            },
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "thoughtsTokenCount": 2,
                "totalTokenCount": 11,
                "cachedContentTokenCount": 3,
            },
        },
    )

    message = body["choices"][0]["message"]
    assert message["content"] == "结果"
    assert message["reasoning_content"] == "先分析"
    assert message["extra_content"]["google"]["thought_signature"] == "sig-thought"
    assert message["images"] == [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/webp;base64,aW1hZ2U="},
        }
    ]
    assert message["tool_calls"][0]["extra_content"] == {
        "google": {"thought_signature": "sig-tool"}
    }
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 6,
        "total_tokens": 11,
        "prompt_tokens_details": {"cached_tokens": 3},
        "completion_tokens_details": {"reasoning_tokens": 2},
    }


def test_gemini_sse_preserves_reasoning_media_tool_calls_and_usage() -> None:
    rendered = b"".join(
        transform_sse(
            "gemini",
            "openai_chat",
            [
                b'data: {"responseId":"stream-1","modelVersion":"gemini-2.5-pro","candidates":[{"content":{"parts":[{"text":"think","thought":true,"thoughtSignature":"sig-thought"},{"text":"answer"},{"inlineData":{"mimeType":"image/png","data":"aW1hZ2U="}},{"functionCall":{"id":"call-1","name":"lookup","args":{"q":"x"}},"thoughtSignature":"sig-tool"}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":2,"candidatesTokenCount":3,"totalTokenCount":5}}\n\n'
            ],
        )
    )

    assert b'"reasoning_content":"think"' in rendered
    assert b'"content":"answer"' in rendered
    assert b'"images"' in rendered
    assert b'data:image/png;base64,aW1hZ2U=' in rendered
    assert b'"tool_calls"' in rendered
    assert b'"thought_signature":"sig-tool"' in rendered
    assert b'"finish_reason":"tool_calls"' in rendered
    assert b'"prompt_tokens":2' in rendered
