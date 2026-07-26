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

    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.public_snapshot()["providers"]["codex"]["api_key"] == "••••••••"
    assert saved["listen_port"] == 7888
    assert "secret" in store.path.read_text(encoding="utf-8")


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
