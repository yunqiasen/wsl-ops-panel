import os
from pathlib import Path
from fastapi.testclient import TestClient
from app.core.security import COOKIE_NAME, issue_session_token
from app.services.agent_mcp import agent_data_root
from app.services.agent_providers import AgentProviderStore
from app.services.agent_router_config import AgentRouterConfigStore
from tests.app_factory import create_app
from tests.test_agent_workbench import _write_agent_category


def test_emit_fixture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for name in [".codex", ".claude"]:
        (home / name).mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "fixture"\n')
    (home / ".claude/settings.json").write_text('{"env":{}}')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    _write_agent_category(tmp_path / "config")
    root = agent_data_root(tmp_path / "config")
    store = AgentProviderStore(root)
    router = AgentRouterConfigStore(root)
    for appid in ["codex", "claude"]:
        settings = (
            {"auth": {}, "config": 'model = "fixture"\n'}
            if appid == "codex"
            else {"env": {"ANTHROPIC_MODEL": "fixture"}}
        )
        store.upsert_provider(
            app_id=appid,
            provider_id=appid + "-provider",
            name=appid + " fixture",
            settings=settings,
            is_current=True,
        )
        router.set_provider(
            appid,
            {
                "base_url": "https://example.test",
                "api_format": "openai_chat",
                "auth_mode": "none",
            },
            provider_id=appid + "-provider",
        )
    store.upsert_provider(
        app_id="codex",
        provider_id="codex-second",
        name="second fixture",
        settings={"auth": {}, "config": 'model = "second"\n'},
    )
    app = create_app(config_root=tmp_path / "config", node_scanner=lambda: [])
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    response = client.get("/categories/agent")
    assert response.status_code == 200, response.text[:100]
    Path(os.environ["ARCHITECTURE_REVIEW_OUTPUT"]).joinpath("fixture.html").write_text(
        response.text
    )
