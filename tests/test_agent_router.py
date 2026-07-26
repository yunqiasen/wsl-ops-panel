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
    from app.main import create_app
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
    from app.main import create_app
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
