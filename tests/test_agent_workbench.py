import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.services.agent_clients import AGENT_CLIENTS
from app.services.agent_mcp import AgentMcpStore, import_mcp_from_home
from app.services.agent_prompts import AgentPromptStore, build_prompt_apply_shell
from app.services.agent_profiles import AgentProfileStore
from app.services.agent_skills import scan_agent_skills


def _write_agent_category(root: Path) -> None:
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "categories" / "agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (root / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )


def test_agent_clients_define_config_targets() -> None:
    by_id = {client.id: client for client in AGENT_CLIENTS}

    assert by_id["codex"].mcp_path == "~/.codex/config.toml"
    assert by_id["codex"].prompt_file == "~/.codex/AGENTS.md"
    assert by_id["claude"].prompt_file == "~/.claude/CLAUDE.md"
    assert by_id["gemini"].mcp_path == "~/.gemini/settings.json"
    assert {"mcp", "prompts", "skills"} <= set(by_id["codex"].features)


def test_import_mcp_from_home_reads_codex_claude_and_gemini(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "node"\nargs = ["server.js"]\n', encoding="utf-8"
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {"mcpServers": {"claude-demo": {"command": "uvx", "args": ["tool"]}}}
        ),
        encoding="utf-8",
    )
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps(
            {"mcpServers": {"gemini-demo": {"url": "https://example.test/mcp"}}}
        ),
        encoding="utf-8",
    )

    servers = import_mcp_from_home(home)

    assert servers["demo"]["apps"]["codex"] is True
    assert servers["demo"]["spec"]["command"] == "node"
    assert servers["claude-demo"]["apps"]["claude"] is True
    assert servers["gemini-demo"]["apps"]["gemini"] is True


def test_import_mcp_from_home_ignores_openclaw_like_cc_switch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.codex-demo]\ncommand = "node"\n', encoding="utf-8"
    )
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"mcpServers": {"claw-demo": {"command": "uvx"}}}), encoding="utf-8"
    )

    servers = import_mcp_from_home(home, apps=["openclaw"])

    assert servers == {}


def test_mcp_apply_shell_writes_supported_client_configs(tmp_path: Path) -> None:
    import os
    import subprocess

    servers = {
        "demo": {
            "id": "demo",
            "spec": {"command": "node", "args": ["server.js"]},
            "apps": {"codex": True},
        }
    }
    from app.services.agent_mcp import build_mcp_apply_shell

    command = build_mcp_apply_shell(
        servers, ["codex", "claude", "gemini"], windows=False
    )
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        cwd="/home/div/1_Project_dir/AI/wsl-ops-panel",
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[mcp_servers.demo]" in (tmp_path / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    )
    assert (
        json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))[
            "mcpServers"
        ]["demo"]["command"]
        == "node"
    )
    assert json.loads(
        (tmp_path / ".gemini" / "settings.json").read_text(encoding="utf-8")
    )["mcpServers"]["demo"]["args"] == ["server.js"]


def test_agent_mcp_store_imports_and_persists(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "node"\n', encoding="utf-8"
    )
    store = AgentMcpStore(tmp_path / "data" / "agent")

    imported = store.import_from_home(home)
    reloaded = AgentMcpStore(tmp_path / "data" / "agent").list_servers()

    assert imported == 1
    assert reloaded["demo"]["apps"]["codex"] is True


def test_prompt_apply_shell_uses_client_prompt_file_and_backup() -> None:
    command = build_prompt_apply_shell("codex", "demo prompt", windows=False)

    assert "~/.codex/AGENTS.md" in command
    assert "wsl-ops-agent-bak" in command
    assert "demo prompt" in command


def test_skill_scan_counts_known_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex" / "skills" / "demo").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)

    result = scan_agent_skills(home)

    assert result["codex"]["count"] == 1
    assert result["codex"]["items"][0]["name"] == "demo"
    assert result["claude"]["count"] == 0


def test_agent_active_client_prefers_config_detected_over_binary_only(tmp_path: Path) -> None:
    from app.services.agent_workbench import build_agent_workbench_context

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")

    context = build_agent_workbench_context(
        tmp_path / "config",
        home=home,
        which=lambda name: f"/usr/bin/{name}",
    )

    assert context["agent_active_client"] == "codex"


def test_agent_category_renders_workbench(tmp_path: Path, monkeypatch) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("model = \"gpt-5\"\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/nonexistent")
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "demo", {"command": "node"}
    )
    AgentPromptStore(tmp_path / "data" / "agent").upsert_prompt(
        "default", "默认提示词", "hello"
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert "Agent 工作台" in response.text
    assert "agent-shell" in response.text
    assert "agent-editor" in response.text
    assert 'data-agent-tab-control="route"' in response.text
    assert 'data-agent-tab-control="skills"' in response.text
    assert 'data-agent-tab-control="profiles"' in response.text
    assert "data-agent-library" in response.text
    assert "data-agent-discovery" in response.text
    assert "data-agent-resource-install" in response.text
    assert "data-agent-resource-uninstall" in response.text
    assert "data-agent-resource-sync" in response.text
    assert "data-agent-resource-delete" in response.text
    assert "data-agent-profile-save" in response.text
    assert "data-agent-app" in response.text
    assert 'value="codex"' in response.text
    assert "data-agent-scope-device" not in response.text
    assert "data-agent-mcp-matrix" not in response.text
    assert "执行设备" not in response.text
    assert "未检测" not in response.text


def test_agent_category_renders_saved_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    AgentProfileStore(tmp_path / "data/agent").upsert(
        "daily", "日常开发", items=[]
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-profile-card="daily"' in response.text


def test_agent_mcp_import_api_and_sync_queue(tmp_path: Path, monkeypatch) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "node"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))

    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    imported = client.post("/api/agent/mcp/import-local")
    assert imported.status_code == 200
    assert imported.json()["imported_count"] == 1

    queued = client.post(
        "/api/agent/mcp/sync",
        json={"server_ids": ["demo"], "apps": ["codex"], "node_ids": ["__local__"]},
    )
    assert queued.status_code == 202
    assert queued.json()["queued_count"] == 1
    assert store.list_all()[0].action == "agent_mcp_sync"


def test_local_mcp_install_and_uninstall_execute_selected_client(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        'model = "keep"\n[mcp_servers.old]\ncommand = "old"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "context7", {"command": "npx", "args": ["context7"]}, {"codex": True}
    )

    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    install = client.post(
        "/api/agent/mcp/local/install",
        json={"client_id": "codex", "mcp_ids": ["context7"]},
    )
    assert install.status_code == 200
    assert install.json()["added"] == ["context7"]
    assert install.json()["updated"] == []
    assert install.json()["removed"] == []
    assert install.json()["verified"] is True

    uninstall = client.post(
        "/api/agent/mcp/local/uninstall",
        json={"client_id": "codex", "mcp_ids": ["context7"]},
    )
    assert uninstall.status_code == 200
    assert uninstall.json()["removed"] == ["context7"]
    assert uninstall.json()["verified"] is True
    assert "old" in (home / ".codex" / "config.toml").read_text(encoding="utf-8")

    from app.services.state_store import PanelStateStore

    observations = PanelStateStore(tmp_path / "data" / "agent").list_mcp_observations(
        "__local__", "codex"
    )
    assert all(item["mcp_id"] != "context7" for item in observations)


def test_agent_prompt_api_queues_apply(tmp_path: Path) -> None:
    _write_agent_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    prompt_store = AgentPromptStore(tmp_path / "data" / "agent")
    prompt_store.upsert_prompt("default", "默认提示词", "hello agent")
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/agent/prompts/apply",
        json={"prompt_id": "default", "apps": ["codex"], "node_ids": ["__local__"]},
    )

    assert response.status_code == 202
    assert response.json()["queued_count"] == 1
    assert store.list_all()[0].action == "agent_prompt_apply"


def test_agent_mcp_and_prompt_store_can_delete(tmp_path: Path) -> None:
    mcp_store = AgentMcpStore(tmp_path / "data" / "agent")
    mcp_store.upsert_server("demo", {"command": "node"}, {"codex": True})
    prompt_store = AgentPromptStore(tmp_path / "data" / "agent")
    prompt_store.upsert_prompt("default", "默认提示词", "hello")

    assert mcp_store.delete_server("demo") is True
    assert prompt_store.delete_prompt("default") is True
    assert mcp_store.list_servers() == {}
    assert prompt_store.list_prompts() == {}


def test_skill_install_and_delete_shell_manage_skill_directory(tmp_path: Path) -> None:
    import os
    import subprocess

    from app.services.agent_skills import (
        build_skill_delete_shell,
        build_skill_install_shell,
    )

    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")

    install = build_skill_install_shell(
        "codex", "demo-skill", str(source), windows=False
    )
    delete = build_skill_delete_shell("codex", "demo-skill", windows=False)
    assert install is not None
    assert delete is not None

    installed = subprocess.run(
        ["bash", "-lc", install],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    assert (tmp_path / ".codex" / "skills" / "demo-skill" / "SKILL.md").exists()

    deleted = subprocess.run(
        ["bash", "-lc", delete],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert deleted.returncode == 0, deleted.stderr
    assert not (tmp_path / ".codex" / "skills" / "demo-skill").exists()
    assert any((tmp_path / ".wsl-ops-agent-backups" / "skills").iterdir())


def test_agent_skill_api_queues_install_update_and_delete(tmp_path: Path) -> None:
    _write_agent_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    install = client.post(
        "/api/agent/skills/install",
        json={
            "skill_name": "demo",
            "source": "/tmp/demo",
            "apps": ["codex"],
            "node_ids": ["__local__"],
        },
    )
    update = client.post(
        "/api/agent/skills/update",
        json={"skill_name": "demo", "apps": ["codex"], "node_ids": ["__local__"]},
    )
    delete = client.post(
        "/api/agent/skills/delete",
        json={"skill_name": "demo", "apps": ["codex"], "node_ids": ["__local__"]},
    )

    assert install.status_code == 202
    assert update.status_code == 202
    assert delete.status_code == 202
    assert [task.action for task in store.list_all()] == [
        "agent_skill_install",
        "agent_skill_update",
        "agent_skill_delete",
    ]


def test_agent_category_renders_skill_actions_and_delete_buttons(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "demo", {"command": "node"}, {"codex": True}
    )
    AgentPromptStore(tmp_path / "data" / "agent").upsert_prompt(
        "default", "默认提示词", "hello"
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert "data-agent-skill-import-source" in response.text
    assert "data-agent-resource-install" in response.text
    assert "data-agent-resource-uninstall" in response.text
    assert "data-agent-resource-sync" in response.text
    assert "data-agent-mcp-delete" in response.text
    assert "data-agent-prompt-delete" in response.text
    assert "data-agent-prompt-save" in response.text
    assert "data-agent-prompt-import-current" in response.text
    assert "data-agent-prompt-restore" in response.text
    assert "data-agent-skill-update" not in response.text
    assert "data-agent-skill-mode" not in response.text


def test_agent_provider_import_and_apply_shell(tmp_path: Path) -> None:
    import os
    import subprocess

    from app.services.agent_providers import (
        AgentProviderStore,
        build_provider_apply_shell,
        import_providers_from_home,
    )

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    providers = import_providers_from_home(home)

    assert providers[0]["app_id"] == "codex"
    assert providers[0]["settings"]["config"] == 'model = "gpt-5"\n'

    store = AgentProviderStore(tmp_path / "data" / "agent")
    assert store.import_from_home(home) == 1
    provider = store.get_provider("codex", "default")
    assert provider is not None

    command = build_provider_apply_shell(provider, windows=False)
    assert command is not None
    target_home = tmp_path / "target-home"
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(target_home)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (target_home / ".codex" / "config.toml").read_text(
        encoding="utf-8"
    ) == 'model = "gpt-5"\n'


def test_provider_meta_round_trips_through_provider_store(tmp_path: Path) -> None:
    from app.services.agent_providers import AgentProviderStore

    store = AgentProviderStore(tmp_path / "data" / "agent")
    saved = store.upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={"type": "codex"},
        meta={"model": "gpt-5.6", "api_format": "openai_responses"},
    )

    assert saved["meta"] == {
        "model": "gpt-5.6",
        "api_format": "openai_responses",
    }
    assert store.get_provider("codex", "relay")["meta"] == saved["meta"]


def test_provider_migration_moves_secret_reference_and_value(tmp_path: Path) -> None:
    import json

    from app.services.agent_providers import AgentProviderStore

    data_root = tmp_path / "data" / "agent"
    store = AgentProviderStore(data_root)
    saved = store.upsert_provider(
        app_id="codex",
        provider_id="local-current",
        name="Legacy",
        settings={
            "type": "codex",
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_key": "legacy-secret",
            },
        },
        is_current=True,
    )
    assert saved["settings_config"]["routing"]["secret_ref"] == (
        "provider-secret:codex:local-current"
    )
    store._state.close()

    restarted = AgentProviderStore(data_root)

    migrated = restarted.get_provider("codex", "default")
    assert migrated is not None
    assert migrated["settings_config"]["routing"]["secret_ref"] == (
        "provider-secret:codex:default"
    )
    assert restarted.provider_for_apply(
        "codex", "default", include_secrets=True
    )["settings_config"]["routing"]["api_key"] == "legacy-secret"
    secrets = json.loads((data_root / "provider-secrets.json").read_text())
    assert secrets["provider-secret:codex:default"] == {"api_key": "legacy-secret"}
    assert "provider-secret:codex:local-current" not in secrets


def test_provider_migration_preserves_conflicting_secret_value(tmp_path: Path) -> None:
    import json

    from app.services.agent_providers import AgentProviderStore

    data_root = tmp_path / "data" / "agent"
    store = AgentProviderStore(data_root)
    store.upsert_provider(
        app_id="codex",
        provider_id="local-current",
        name="Legacy",
        settings={
            "type": "codex",
            "routing": {"api_key": "legacy-secret"},
        },
    )
    store._secrets.put(
        "codex", "default", {"api_key": "preexisting-default-secret"}
    )
    store._state.close()

    AgentProviderStore(data_root)

    secrets = json.loads((data_root / "provider-secrets.json").read_text())
    assert secrets["provider-secret:codex:default"] == {"api_key": "legacy-secret"}
    assert {"api_key": "preexisting-default-secret"} in secrets.values()


def test_agent_provider_api_import_save_apply_queue(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    imported = client.post("/api/agent/providers/import-local")
    assert imported.status_code == 200
    assert imported.json()["imported_count"] == 1

    saved = client.post(
        "/api/agent/providers",
        json={
            "app_id": "codex",
            "provider_id": "manual",
            "name": "Manual Codex",
            "settings_config": {"type": "codex", "config": 'model = "gpt-5-mini"\n'},
            "is_current": True,
        },
    )
    assert saved.status_code == 200

    queued = client.post(
        "/api/agent/providers/apply",
        json={"app_id": "codex", "provider_id": "manual", "node_ids": ["__local__"]},
    )
    assert queued.status_code == 202
    assert queued.json()["queued_count"] == 1
    assert store.list_all()[0].action == "agent_provider_apply"


def test_provider_import_rejects_router_takeover_without_saving_proxy_config(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore
    from app.services.agent_router_control import AgentRouterController

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'model_provider = "original"\n'
        '[model_providers.original]\n'
        'base_url = "https://upstream.example/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    data_root = tmp_path / "data" / "agent"
    controller = AgentRouterController(
        AgentRouterConfigStore(data_root),
        home=home,
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    app = create_app(config_root=tmp_path, node_scanner=lambda: [])
    app.state.agent_router_controller = controller
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    controller.enable_takeover("codex")
    taken_over = config.read_text(encoding="utf-8")

    response = client.post(
        "/api/agent/providers/import-local", json={"apps": ["codex"]}
    )

    assert response.status_code == 409, response.text
    assert "接管" in response.json()["detail"]
    assert AgentProviderStore(data_root).get_provider("codex", "local-current") is None
    assert config.read_text(encoding="utf-8") == taken_over


def test_provider_direct_apply_rejects_local_client_under_router_takeover(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore
    from app.services.agent_router_control import AgentRouterController
    from app.tasks.store import InMemoryTaskStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    config = home / ".codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'model_provider = "original"\n'
        '[model_providers.original]\n'
        'base_url = "https://upstream.example/v1"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    data_root = tmp_path / "data" / "agent"
    AgentProviderStore(data_root).upsert_provider(
        app_id="codex",
        provider_id="direct",
        name="Direct",
        settings={
            "routing": {
                "base_url": "https://direct.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "none",
            }
        },
    )
    controller = AgentRouterController(
        AgentRouterConfigStore(data_root),
        home=home,
        runner=lambda command: 0,
        health_probe=lambda: {"status": "ok"},
    )
    tasks = InMemoryTaskStore()
    app = create_app(
        config_root=tmp_path, task_store=tasks, node_scanner=lambda: []
    )
    app.state.agent_router_controller = controller
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    controller.enable_takeover("codex")
    taken_over = config.read_text(encoding="utf-8")

    response = client.post(
        "/api/agent/providers/apply",
        json={
            "app_id": "codex",
            "provider_id": "direct",
            "node_ids": ["__local__"],
        },
    )

    assert response.status_code == 409, response.text
    assert "接管" in response.json()["detail"]
    assert tasks.list_all() == []
    assert config.read_text(encoding="utf-8") == taken_over


def test_same_mcp_id_with_different_client_specs_uses_database_variants(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "node"\n', encoding="utf-8"
    )
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"demo": {"command": "uvx", "args": ["demo"]}}}),
        encoding="utf-8",
    )
    store = AgentMcpStore(tmp_path / "data/agent")

    store.import_from_home(home, apps=["codex", "claude"])

    assert set(store.list_servers()) == {"demo"}
    assert store.get_server("demo")["apps"] == {"claude": True, "codex": True}
    variants = store.state.list_mcp_variants("demo")
    assert {(item["client_id"], item["spec"]["command"]) for item in variants} == {
        ("codex", "node"),
        ("claude", "uvx"),
    }


def test_agent_page_and_provider_api_never_return_raw_secret(tmp_path: Path) -> None:
    _write_agent_category(tmp_path)
    from app.services.agent_providers import AgentProviderStore, REDACTED_SECRET

    AgentProviderStore(tmp_path / "data" / "agent").upsert_provider(
        app_id="claude",
        provider_id="secret-demo",
        name="Secret Demo",
        settings={
            "type": "claude",
            "config": {"env": {"ANTHROPIC_API_KEY": "raw-secret-value"}},
        },
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    page = client.get("/categories/agent")
    detail = client.get("/api/agent/providers/claude/secret-demo")

    assert page.status_code == 200
    assert "raw-secret-value" not in page.text
    assert "data-agent-provider-json" not in page.text
    assert detail.status_code == 200
    assert "raw-secret-value" not in detail.text
    assert (
        detail.json()["settings_config"]["config"]["env"]["ANTHROPIC_API_KEY"]
        == REDACTED_SECRET
    )


def test_provider_apply_without_write_secrets_removes_nested_credentials(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from app.services.agent_providers import build_provider_apply_shell

    provider = {
        "id": "demo",
        "app_id": "claude",
        "settings_config": {
            "type": "claude",
            "config": {
                "model": "claude-sonnet",
                "env": {"ANTHROPIC_API_KEY": "secret", "SAFE_FLAG": "1"},
            },
        },
    }
    command = build_provider_apply_shell(provider, write_secrets=False)
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))
    assert payload["model"] == "claude-sonnet"
    assert payload["env"] == {"SAFE_FLAG": "1"}


def test_hermes_mcp_sync_preserves_unrelated_config(tmp_path: Path) -> None:
    import os
    import subprocess

    from app.services.agent_mcp import build_mcp_apply_shell

    (tmp_path / ".hermes").mkdir(parents=True)
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "model: demo\ntemperature: 0.4\n", encoding="utf-8"
    )
    command = build_mcp_apply_shell(
        {
            "demo": {
                "id": "demo",
                "spec": {"command": "node", "args": ["server.js"]},
                "apps": {"hermes": True},
            }
        },
        ["hermes"],
    )
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        cwd="/home/div/1_Project_dir/AI/wsl-ops-panel",
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    import yaml

    payload = yaml.safe_load(
        (tmp_path / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    )
    assert payload["model"] == "demo"
    assert payload["temperature"] == 0.4
    assert payload["mcp_servers"]["demo"]["command"] == "node"


def test_mcp_adapters_incrementally_preserve_existing_entries_and_unrelated_fields(
    tmp_path: Path,
) -> None:
    from app.services.agent_mcp_adapters import apply_mcp_to_home, scan_mcp_home

    (tmp_path / ".codex").mkdir(parents=True)
    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "keep"\n[mcp_servers.old]\ncommand = "old"\n', encoding="utf-8"
    )
    (tmp_path / ".gemini").mkdir(parents=True)
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"theme": "keep", "mcpServers": {"old": {"command": "old"}}}),
        encoding="utf-8",
    )

    apply_mcp_to_home(tmp_path, "codex", {"new": {"command": "new"}})
    apply_mcp_to_home(tmp_path, "gemini", {"new": {"command": "new"}})

    assert set(scan_mcp_home(tmp_path, "codex")) == {"old", "new"}
    assert set(scan_mcp_home(tmp_path, "gemini")) == {"old", "new"}
    assert (
        json.loads((tmp_path / ".gemini" / "settings.json").read_text())["theme"]
        == "keep"
    )


def test_mcp_adapters_handle_opencode_and_hermes(tmp_path: Path) -> None:
    from app.services.agent_mcp_adapters import apply_mcp_to_home, scan_mcp_home

    apply_mcp_to_home(
        tmp_path,
        "opencode",
        {"demo": {"type": "stdio", "command": "npx", "args": ["demo"]}},
    )
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "config.yaml").write_text("model: keep\n", encoding="utf-8")
    apply_mcp_to_home(
        tmp_path, "hermes", {"demo": {"command": "npx", "args": ["demo"]}}
    )

    assert scan_mcp_home(tmp_path, "opencode")["demo"]["command"] == "npx"
    assert scan_mcp_home(tmp_path, "hermes")["demo"]["command"] == "npx"
    import yaml

    assert (
        yaml.safe_load((tmp_path / ".hermes" / "config.yaml").read_text())["model"]
        == "keep"
    )


def test_truthful_inventory_uses_real_client_configs_not_legacy_app_flags(
    tmp_path: Path,
) -> None:
    from app.services.agent_workbench import build_agent_workbench_context

    config_root = tmp_path / "config"
    config_root.mkdir()
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8"
    )
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "fake", {"command": "fake"}, {"codex": True, "openclaw": True}
    )

    context = build_agent_workbench_context(config_root, home=home)
    rows = {row["id"]: row for row in context["agent_mcp_servers"]}
    discovered = {row["id"]: row for row in context["agent_mcp_discovery"]}

    assert "context7" not in rows
    assert discovered["context7"]["observations"]["codex"]["status"] == "installed"
    assert discovered["context7"]["managed"] is False
    assert rows["fake"]["observations"].get("codex") is None
    assert rows["fake"]["status"] == "unscanned"


def test_incremental_mcp_apply_shell_preserves_existing_mcp(tmp_path: Path) -> None:
    import os
    import subprocess
    from app.services.agent_mcp import build_mcp_apply_shell

    (tmp_path / ".gemini").mkdir(parents=True)
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"theme": "keep", "mcpServers": {"old": {"command": "old"}}}),
        encoding="utf-8",
    )
    command = build_mcp_apply_shell(
        {"new": {"id": "new", "spec": {"command": "new"}, "apps": {}}}, ["gemini"]
    )
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert set(payload["mcpServers"]) == {"old", "new"}
    assert payload["theme"] == "keep"


def test_mcp_preview_and_apply_support_different_client_assignments(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore

    task_store = InMemoryTaskStore()
    mcp_store = AgentMcpStore(tmp_path / "data" / "agent")
    mcp_store.upsert_server("context7", {"command": "npx"}, {"codex": True})
    mcp_store.upsert_server(
        "deepwiki",
        {"type": "http", "url": "https://example.test/mcp"},
        {"claude": True},
    )
    client = TestClient(
        create_app(config_root=tmp_path, task_store=task_store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())
    assignments = [
        {"node_id": "__local__", "client_id": "codex", "mcp_ids": ["context7"]},
        {"node_id": "__local__", "client_id": "claude", "mcp_ids": ["deepwiki"]},
    ]

    preview = client.post("/api/agent/mcp/preview", json={"assignments": assignments})
    applied = client.post("/api/agent/mcp/apply", json={"assignments": assignments})

    assert preview.status_code == 200
    assert {item["client_id"] for item in preview.json()["targets"]} == {
        "codex",
        "claude",
    }
    assert applied.status_code == 202
    assert applied.json()["queued_count"] == 1
    assert task_store.list_all()[0].action == "agent_mcp_apply"


def test_mcp_uninstall_preview_uses_observations_without_local_library_definition(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    from app.services.state_store import PanelStateStore

    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    PanelStateStore(tmp_path / "data" / "agent").replace_mcp_observations(
        "__local__",
        "codex",
        [
            {
                "mcp_id": "observed-only",
                "present": True,
                "status": "installed",
                "public_spec": {"command": "npx"},
            }
        ],
    )

    ready = client.post(
        "/api/agent/mcp/preview",
        json={
            "action": "uninstall",
            "assignments": [
                {
                    "node_id": "__local__",
                    "client_id": "codex",
                    "mcp_ids": ["observed-only"],
                }
            ],
        },
    )
    missing = client.post(
        "/api/agent/mcp/preview",
        json={
            "action": "uninstall",
            "assignments": [
                {
                    "node_id": "__local__",
                    "client_id": "codex",
                    "mcp_ids": ["not-installed"],
                }
            ],
        },
    )

    assert ready.status_code == 200
    assert ready.json()["targets"][0]["status"] == "ready"
    assert ready.json()["targets"][0]["action"] == "uninstall"
    assert missing.status_code == 200
    assert missing.json()["targets"][0]["status"] == "invalid"
    assert "未观测为已安装" in missing.json()["targets"][0]["reason"]


def test_agent_page_renders_equal_scope_buttons_and_truthful_status_without_fake_dots(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8"
    )
    AgentMcpStore(tmp_path / "data/agent").import_from_home(home, apps=["codex"])
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert "data-agent-app" in response.text
    assert 'value="codex"' in response.text
    assert 'aria-pressed="true"' in response.text
    assert 'data-agent-tab-control="route"' in response.text
    assert "data-agent-mcp-install" in response.text
    assert "data-agent-mcp-matrix" not in response.text
    assert "agent-app-dot" not in response.text
    assert "data-agent-scope-client" not in response.text
    assert "context7" in response.text
    assert "已安装" in response.text
    assert 'data-agent-mcp-install-one="context7"' in response.text


def test_mcp_scan_imports_real_local_config_and_reports_targets(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/agent/mcp/scan", json={"node_ids": ["__local__"], "apps": ["codex"]}
    )

    assert response.status_code == 200
    assert response.json()["targets"][0]["status"] == "scanned"
    assert response.json()["targets"][0]["mcp_ids"] == ["context7"]
    store = AgentMcpStore(tmp_path / "data" / "agent")
    assert store.get_server("context7") is None
    observations = store.state.list_mcp_observations("__local__", "codex")
    assert observations[0]["mcp_id"] == "context7"
    assert observations[0]["status"] == "installed"


def test_remote_mcp_scan_shell_returns_client_inventory(tmp_path: Path) -> None:
    import os
    import subprocess
    from app.services.agent_mcp import build_mcp_scan_shell, parse_mcp_scan_output

    (tmp_path / ".codex").mkdir(parents=True)
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8"
    )
    command = build_mcp_scan_shell(["codex"])
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert parse_mcp_scan_output(result.stdout)["codex"]["context7"]["command"] == "npx"


def test_agent_context_keeps_observations_by_device_and_client(tmp_path: Path) -> None:
    from app.services.agent_workbench import build_agent_workbench_context
    from app.services.state_store import PanelStateStore

    config_root = tmp_path / "config"
    config_root.mkdir()
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "remote-demo", {"command": "npx"}, {}
    )
    PanelStateStore(config_root).replace_mcp_observations(
        "mac-book",
        "claude",
        [
            {
                "mcp_id": "remote-demo",
                "status": "installed",
                "present": True,
                "public_spec": {"command": "npx"},
            }
        ],
    )

    context = build_agent_workbench_context(config_root, home=tmp_path / "empty-home")
    row = next(
        item for item in context["agent_mcp_servers"] if item["id"] == "remote-demo"
    )

    assert row["observations_by_target"]["mac-book"]["claude"]["status"] == "installed"


def test_mcp_inventory_api_returns_observations_not_legacy_targets(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "legacy", {"command": "node"}, {"codex": True}
    )
    from app.services.state_store import PanelStateStore

    PanelStateStore(tmp_path).replace_mcp_observations(
        "__local__",
        "codex",
        [
            {
                "mcp_id": "real",
                "status": "installed",
                "present": True,
                "public_spec": {"command": "npx"},
            }
        ],
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/api/agent/mcp/inventory")

    assert response.status_code == 200
    assert response.json()["observations"][0]["mcp_id"] == "real"
    assert response.json()["observations"][0]["status"] == "installed"


def test_remove_mcp_from_home_preserves_other_client_configuration(tmp_path: Path) -> None:
    from app.services.agent_mcp_adapters import remove_mcp_from_home, scan_mcp_home

    (tmp_path / ".codex").mkdir(parents=True)
    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "keep"\n[mcp_servers.remove]\ncommand = "remove"\n[mcp_servers.keep]\ncommand = "keep"\n',
        encoding="utf-8",
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps({"theme": "keep", "mcpServers": {"remove": {"command": "x"}, "keep": {"command": "y"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".gemini").mkdir(parents=True)
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"theme": "keep", "mcpServers": {"remove": {"command": "x"}, "keep": {"command": "y"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".config" / "opencode").mkdir(parents=True)
    (tmp_path / ".config" / "opencode" / "opencode.json").write_text(
        json.dumps({"theme": "keep", "mcp": {"remove": {"type": "local", "command": ["x"]}, "keep": {"type": "remote", "url": "https://example.test/mcp"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".openclaw").mkdir(parents=True)
    (tmp_path / ".openclaw" / "openclaw.json").write_text(
        json.dumps({"theme": "keep", "servers": {"remove": {"command": "x"}, "keep": {"command": "y"}}}),
        encoding="utf-8",
    )
    (tmp_path / ".hermes").mkdir(parents=True)
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "model: keep\nmcp_servers:\n  remove:\n    command: x\n  keep:\n    command: y\n",
        encoding="utf-8",
    )

    for client_id in ("codex", "claude", "gemini", "opencode", "hermes"):
        remove_mcp_from_home(tmp_path, client_id, {"remove"})
        assert set(scan_mcp_home(tmp_path, client_id)) == {"keep"}

    assert 'model = "keep"' in (tmp_path / ".codex" / "config.toml").read_text()
    assert json.loads((tmp_path / ".claude.json").read_text())["theme"] == "keep"
    assert json.loads((tmp_path / ".gemini" / "settings.json").read_text())["theme"] == "keep"
    assert json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())["theme"] == "keep"
    assert json.loads((tmp_path / ".openclaw" / "openclaw.json").read_text())["theme"] == "keep"
    import yaml

    assert yaml.safe_load((tmp_path / ".hermes" / "config.yaml").read_text())["model"] == "keep"


def test_apply_mcp_to_home_fails_when_readback_content_differs(
    tmp_path: Path, monkeypatch
) -> None:
    import app.services.agent_mcp_adapters as adapters

    real_scan = adapters.scan_mcp_home
    scans = 0

    def stale_scan(home: Path, client_id: str) -> dict[str, dict]:
        nonlocal scans
        scans += 1
        if scans == 1:
            return real_scan(home, client_id)
        return {"demo": {"type": "stdio", "command": "stale"}}

    monkeypatch.setattr(adapters, "scan_mcp_home", stale_scan)

    import pytest

    with pytest.raises(RuntimeError, match="readback verification failed"):
        adapters.apply_mcp_to_home(
            tmp_path,
            "claude",
            {"demo": {"type": "stdio", "command": "expected"}},
        )


def test_remove_mcp_from_home_fails_when_readback_still_contains_removed_id(
    tmp_path: Path, monkeypatch
) -> None:
    import app.services.agent_mcp_adapters as adapters

    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"remove": {"command": "x"}}}), encoding="utf-8"
    )
    real_scan = adapters.scan_mcp_home
    scans = 0

    def stale_scan(home: Path, client_id: str) -> dict[str, dict]:
        nonlocal scans
        scans += 1
        if scans == 1:
            return real_scan(home, client_id)
        return {"remove": {"command": "x"}}

    monkeypatch.setattr(adapters, "scan_mcp_home", stale_scan)

    import pytest

    with pytest.raises(RuntimeError, match="readback verification failed"):
        adapters.remove_mcp_from_home(tmp_path, "claude", {"remove"})


def test_mcp_remove_shell_removes_only_selected_ids_for_each_client(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    from app.services.agent_mcp import build_mcp_remove_shell

    (tmp_path / ".gemini").mkdir(parents=True)
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"theme": "keep", "mcpServers": {"remove": {"command": "x"}, "keep": {"command": "y"}}}),
        encoding="utf-8",
    )
    command = build_mcp_remove_shell({"remove"}, ["gemini"])
    assert command is not None

    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert set(payload["mcpServers"]) == {"keep"}
    assert payload["theme"] == "keep"
    assert list((tmp_path / ".gemini").glob("settings.json.wsl-ops-agent-bak-*"))


def test_mcp_uninstall_queues_verified_local_target_and_removes_assignment(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    from app.services.state_store import PanelStateStore
    from app.tasks.store import InMemoryTaskStore

    task_store = InMemoryTaskStore()
    mcp_store = AgentMcpStore(tmp_path / "data" / "agent")
    mcp_store.upsert_server("context7", {"command": "npx"}, {"codex": True})
    state = PanelStateStore(tmp_path)
    state.replace_mcp_assignments(
        "__local__", "codex", [{"mcp_id": "context7", "variant_client_id": "codex", "variant_platform": "linux"}]
    )
    state.replace_mcp_observations(
        "__local__", "codex", [{"mcp_id": "context7", "present": True, "status": "installed", "public_spec": {"command": "npx"}}]
    )
    client = TestClient(
        create_app(config_root=tmp_path, task_store=task_store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/agent/mcp/uninstall",
        json={"assignments": [{"node_id": "__local__", "client_id": "codex", "mcp_ids": ["context7"]}]},
    )

    assert response.status_code == 202
    assert response.json()["queued_count"] == 1
    assert task_store.list_all()[0].action == "agent_mcp_uninstall"
    assert state.list_mcp_assignments("__local__", "codex") == []
    operations = state.list_mcp_operations()
    assert operations[0]["action"] == "uninstall"
    assert operations[0]["status"] == "queued"


def test_mcp_uninstall_skips_unobserved_offline_and_windows_targets(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    from app.models.remote_nodes import RemoteNodeCreate
    from app.tasks.store import InMemoryTaskStore

    task_store = InMemoryTaskStore()
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "context7", {"command": "npx"}, {"codex": True}
    )
    client = TestClient(
        create_app(config_root=tmp_path, task_store=task_store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.app.state.remote_node_store.add_node(
        RemoteNodeCreate(name="Offline", host="offline.test", username="div", auth_type="key", key_path="/tmp/id")
    )
    client.app.state.remote_node_store.add_node(
        RemoteNodeCreate(name="Win", host="win.test", username="Administrator", password="pw", os_hint="windows")
    )
    client.app.state.remote_node_store.update_node_status("win", status="online")

    response = client.post(
        "/api/agent/mcp/uninstall",
        json={
            "assignments": [
                {"node_id": "__local__", "client_id": "codex", "mcp_ids": ["context7"]},
                {"node_id": "offline", "client_id": "codex", "mcp_ids": ["context7"]},
                {"node_id": "win", "client_id": "codex", "mcp_ids": ["context7"]},
            ]
        },
    )

    assert response.status_code == 202
    assert response.json()["queued_count"] == 0
    assert len(response.json()["skipped"]) == 3
    assert any("未观测为已安装" in item for item in response.json()["skipped"])
    assert any("SSH 未连接" in item for item in response.json()["skipped"])
    assert any("Windows" in item for item in response.json()["skipped"])
    assert task_store.list_all() == []


def test_agent_mcp_rows_group_installed_clients_and_render_uninstall_actions(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.context7]\ncommand = "npx"\n', encoding="utf-8"
    )
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"context7": {"command": "npx"}}}), encoding="utf-8"
    )
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"mcpServers": {"context7": {"command": "npx"}}}), encoding="utf-8"
    )
    AgentMcpStore(tmp_path / "data/agent").import_from_home(home, apps=["codex", "claude", "gemini"])
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-mcp-managed="true"' in response.text
    assert 'data-agent-mcp-install-one="context7"' in response.text
    assert 'data-agent-mcp-uninstall-one="context7"' in response.text
    assert 'data-agent-mcp-edit="context7"' in response.text
    assert 'data-agent-mcp-json="context7"' in response.text
    assert "data-agent-mcp-matrix" not in response.text
    assert "当前 WSL · 已装 3" not in response.text
    assert "data-agent-mcp-delete" in response.text


def test_agent_mcp_matrix_exposes_install_and_uninstall_modes(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "npx"\n', encoding="utf-8"
    )
    AgentMcpStore(tmp_path / "data/agent").import_from_home(home, apps=["codex"])
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert "data-agent-mcp-matrix" not in response.text
    assert "data-agent-matrix-mode-label" not in response.text
    assert 'data-agent-mcp-install-one="demo"' in response.text
    assert 'data-agent-mcp-uninstall-one="demo"' in response.text
    assert "data-agent-mcp-delete" in response.text


def test_manual_routing_provider_direct_apply_preserves_opencode_config(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from app.services.agent_providers import build_provider_apply_shell

    target = tmp_path / ".config" / "opencode" / "opencode.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"keep": "original", "options": {"unchanged": True}}) + "\n",
        encoding="utf-8",
    )
    provider = {
        "id": "relay-main",
        "app_id": "opencode",
        "settings_config": {
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_chat",
                "auth_mode": "none",
                "model": "relay-model",
            }
        },
    }

    command = build_provider_apply_shell(provider, write_secrets=False)
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["keep"] == "original"
    assert payload["options"] == {
        "unchanged": True,
        "baseURL": "https://relay.example/v1",
    }
    assert payload["model"] == "relay-model"


def test_manual_routing_provider_direct_apply_preserves_claude_and_openclaw_json(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from app.services.agent_providers import build_provider_apply_shell

    cases = {
        "claude": (
            tmp_path / ".claude/settings.json",
            {"keep": "original", "env": {"unchanged": "1"}},
        ),
        "openclaw": (
            tmp_path / ".openclaw" / "openclaw.json",
            {"keep": "original", "unchanged": True},
        ),
    }
    for app_id, (target, original) in cases.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(original) + "\n", encoding="utf-8")
        provider = {
            "id": "relay-main",
            "app_id": app_id,
            "settings_config": {
                "routing": {
                    "base_url": "https://relay.example/v1",
                    "api_format": "openai_chat",
                    "auth_mode": "none",
                    "model": "relay-model",
                }
            },
        }
        command = build_provider_apply_shell(provider, write_secrets=False)
        assert command is not None
        result = subprocess.run(
            ["bash", "-lc", command],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    claude = json.loads(cases["claude"][0].read_text(encoding="utf-8"))
    assert claude["keep"] == "original"
    assert claude["env"] == {
        "unchanged": "1",
        "ANTHROPIC_BASE_URL": "https://relay.example/v1",
        "ANTHROPIC_MODEL": "relay-model",
    }
    openclaw = json.loads(cases["openclaw"][0].read_text(encoding="utf-8"))
    assert openclaw["keep"] == "original"
    assert openclaw["unchanged"] is True
    assert openclaw["baseUrl"] == "https://relay.example/v1"
    assert openclaw["model"] == "relay-model"


def test_manual_routing_provider_direct_apply_preserves_gemini_files(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from app.services.agent_providers import build_provider_apply_shell

    settings_path = tmp_path / ".gemini" / "settings.json"
    env_path = tmp_path / ".gemini" / ".env"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"keep": "original"}) + "\n", encoding="utf-8")
    env_path.write_text("UNCHANGED=1\n", encoding="utf-8")
    provider = {
        "id": "relay-main",
        "app_id": "gemini",
        "settings_config": {
            "routing": {
                "base_url": "https://relay.example/v1beta",
                "api_format": "gemini",
                "auth_mode": "none",
                "model": "relay-model",
            }
        },
    }

    command = build_provider_apply_shell(provider, write_secrets=False)
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(settings_path.read_text(encoding="utf-8")) == {
        "keep": "original"
    }
    env = env_path.read_text(encoding="utf-8")
    assert "UNCHANGED=1" in env
    assert "GOOGLE_GEMINI_BASE_URL=https://relay.example/v1beta" in env
    assert "GEMINI_MODEL=relay-model" in env


def test_manual_routing_provider_direct_apply_preserves_codex_config(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    import tomllib

    from app.services.agent_providers import build_provider_apply_shell

    target = tmp_path / ".codex" / "config.toml"
    target.parent.mkdir(parents=True)
    target.write_text(
        'model = "keep-model"\n'
        'model_provider = "original"\n'
        'approval_policy = "on-request"\n'
        '\n'
        '[model_providers.original]\n'
        'name = "Original"\n'
        'base_url = "https://original.example/v1"\n',
        encoding="utf-8",
    )
    provider = {
        "id": "relay-main",
        "app_id": "codex",
        "name": "Relay Main",
        "settings_config": {
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "none",
                "model": "relay-model",
            }
        },
    }

    command = build_provider_apply_shell(provider, write_secrets=False)
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = tomllib.loads(target.read_text(encoding="utf-8"))
    assert payload["model"] == "relay-model"
    assert payload["model_provider"] == "relay_main"
    assert payload["approval_policy"] == "on-request"
    assert payload["model_providers"]["original"]["base_url"] == (
        "https://original.example/v1"
    )
    assert payload["model_providers"]["relay_main"] == {
        "name": "Relay Main",
        "base_url": "https://relay.example/v1",
        "wire_api": "responses",
        "requires_openai_auth": False,
    }


def test_manual_routing_provider_direct_apply_preserves_hermes_config(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    import yaml

    from app.services.agent_providers import build_provider_apply_shell

    target = tmp_path / ".hermes" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "model: keep-model\n"
        "temperature: 0.4\n"
        "system_prompt: keep-this\n",
        encoding="utf-8",
    )
    provider = {
        "id": "relay-main",
        "app_id": "hermes",
        "settings_config": {
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_chat",
                "auth_mode": "none",
                "model": "relay-model",
            }
        },
    }

    command = build_provider_apply_shell(provider, write_secrets=False)
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert payload["temperature"] == 0.4
    assert payload["system_prompt"] == "keep-this"
    assert payload["base_url"] == "https://relay.example/v1"
    assert payload["api_mode"] == "openai_chat"
    assert payload["model"] == "relay-model"


def test_agent_mcp_editor_id_selector_is_not_reused_by_server_rows(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    AgentMcpStore(tmp_path / "data" / "agent").upsert_server(
        "demo", {"command": "node"}, {"codex": True}
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-mcp-card-id="demo"' in response.text
    assert response.text.count("data-agent-mcp-id") == 1


def test_agent_provider_module_compiles_without_syntax_warnings() -> None:
    import warnings

    source_path = Path("app/services/agent_providers.py")
    source = source_path.read_text(encoding="utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        compile(source, str(source_path), "exec")


def test_codex_mcp_adapter_maps_unified_headers_to_native_http_headers(
    tmp_path: Path,
) -> None:
    import tomllib

    from app.services.agent_mcp_adapters import apply_mcp_to_home, scan_mcp_home

    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('model = "keep"\n', encoding="utf-8")
    expected = {
        "type": "http",
        "url": "https://mcp.example.test",
        "headers": {"Authorization": "Bearer fixture"},
        "timeout": 30,
    }

    apply_mcp_to_home(tmp_path, "codex", {"remote": expected})

    native = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["remote"]
    assert native == {
        "type": "http",
        "url": "https://mcp.example.test",
        "http_headers": {"Authorization": "Bearer fixture"},
        "timeout": 30,
    }
    assert "headers" not in native
    assert scan_mcp_home(tmp_path, "codex")["remote"] == expected


def test_generated_codex_mcp_apply_uses_native_http_headers(tmp_path: Path) -> None:
    import os
    import subprocess
    import tomllib

    from app.services.agent_mcp import build_mcp_apply_shell

    path = tmp_path / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('model = "keep"\n', encoding="utf-8")
    command = build_mcp_apply_shell(
        {
            "remote": {
                "id": "remote",
                "spec": {
                    "type": "http",
                    "url": "https://mcp.example.test",
                    "headers": {"X-Test": "fixture"},
                    "timeout": 30,
                },
                "apps": {"codex": True},
            }
        },
        ["codex"],
    )
    assert command is not None

    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    native = tomllib.loads(path.read_text(encoding="utf-8"))["mcp_servers"]["remote"]
    assert native["type"] == "http"
    assert native["http_headers"] == {"X-Test": "fixture"}
    assert "headers" not in native
    assert native["timeout"] == 30


def test_grokbuild_mcp_adapter_round_trip_preserves_native_toml(tmp_path: Path) -> None:
    import tomllib

    from app.services.agent_mcp_adapters import (
        apply_mcp_to_home,
        remove_mcp_from_home,
        scan_mcp_home,
    )

    path = tmp_path / ".grok" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[models]\ndefault = "main"\n\n'
        '[model.main]\nmodel = "grok-4.5"\nbase_url = "https://api.x.ai/v1"\n'
        'name = "xAI"\nenv_key = "XAI_API_KEY"\napi_backend = "responses"\n'
        'context_window = 500000\n\n'
        '[mcp_servers.old]\ncommand = "old"\nargs = ["serve"]\n',
        encoding="utf-8",
    )

    assert scan_mcp_home(tmp_path, "grokbuild")["old"] == {
        "type": "stdio",
        "command": "old",
        "args": ["serve"],
    }

    apply_mcp_to_home(
        tmp_path,
        "grokbuild",
        {
            "remote": {
                "type": "http",
                "url": "https://mcp.example.test",
                "headers": {"Authorization": "Bearer fixture"},
            }
        },
    )

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["models"]["default"] == "main"
    assert parsed["model"]["main"]["model"] == "grok-4.5"
    assert parsed["mcp_servers"]["remote"]["headers"] == {
        "Authorization": "Bearer fixture"
    }
    assert "type" not in parsed["mcp_servers"]["remote"]
    assert "http_headers" not in parsed["mcp_servers"]["remote"]
    assert scan_mcp_home(tmp_path, "grokbuild")["remote"]["type"] == "http"

    remove_mcp_from_home(tmp_path, "grokbuild", {"remote"})
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    assert set(after["mcp_servers"]) == {"old"}
    assert after["models"]["default"] == "main"


def test_hermes_mcp_adapter_preserves_native_server_fields_on_transport_change(
    tmp_path: Path,
) -> None:
    import yaml

    from app.services.agent_mcp_adapters import (
        apply_mcp_to_home,
        remove_mcp_from_home,
        scan_mcp_home,
    )

    path = tmp_path / ".hermes" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "model:\n"
        "  provider: keep\n"
        "mcp_servers:\n"
        "  demo:\n"
        "    command: npx\n"
        "    args: [demo]\n"
        "    enabled: false\n"
        "    timeout: 90\n"
        "    connect_timeout: 8\n"
        "    tools: [read]\n"
        "    sampling: {temperature: 0.2}\n"
        "    roots: [/workspace]\n"
        "    auth: oauth\n"
        "    future_flag: keep\n"
        "  sibling:\n"
        "    command: sibling\n"
        "    timeout: 12\n",
        encoding="utf-8",
    )

    apply_mcp_to_home(
        tmp_path,
        "hermes",
        {
            "demo": {
                "type": "http",
                "url": "https://mcp.example.test",
                "headers": {"X-Test": "fixture"},
            }
        },
    )

    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    demo = payload["mcp_servers"]["demo"]
    assert payload["model"] == {"provider": "keep"}
    assert demo["url"] == "https://mcp.example.test"
    assert demo["headers"] == {"X-Test": "fixture"}
    assert "command" not in demo
    assert "args" not in demo
    assert demo["enabled"] is False
    assert demo["timeout"] == 90
    assert demo["connect_timeout"] == 8
    assert demo["tools"] == ["read"]
    assert demo["sampling"] == {"temperature": 0.2}
    assert demo["roots"] == ["/workspace"]
    assert demo["auth"] == "oauth"
    assert demo["future_flag"] == "keep"
    assert scan_mcp_home(tmp_path, "hermes")["demo"] == {
        "type": "http",
        "url": "https://mcp.example.test",
        "headers": {"X-Test": "fixture"},
    }

    remove_mcp_from_home(tmp_path, "hermes", {"demo"})
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "demo" not in after["mcp_servers"]
    assert after["mcp_servers"]["sibling"] == {"command": "sibling", "timeout": 12}
    assert after["model"] == {"provider": "keep"}


def test_import_mcp_from_home_reads_grokbuild_native_toml(tmp_path: Path) -> None:
    path = tmp_path / ".grok" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[mcp_servers.remote]\nurl = "https://mcp.example.test"\n'
        'headers = { Authorization = "Bearer fixture" }\n',
        encoding="utf-8",
    )

    servers = import_mcp_from_home(tmp_path, apps=["grokbuild"])

    assert servers["remote"]["apps"] == {"grokbuild": True}
    assert servers["remote"]["spec"] == {
        "type": "http",
        "url": "https://mcp.example.test",
        "headers": {"Authorization": "Bearer fixture"},
    }


def test_grokbuild_generated_mcp_scripts_apply_scan_and_remove(tmp_path: Path) -> None:
    import os
    import subprocess
    import tomllib

    from app.services.agent_mcp import (
        build_mcp_apply_shell,
        build_mcp_remove_shell,
        build_mcp_scan_shell,
        parse_mcp_scan_output,
    )

    path = tmp_path / ".grok" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[models]\ndefault = "main"\n\n[mcp_servers.keep]\ncommand = "keep"\n',
        encoding="utf-8",
    )
    servers = {
        "remote": {
            "id": "remote",
            "spec": {
                "type": "http",
                "url": "https://mcp.example.test",
                "headers": {"X-Test": "fixture"},
            },
            "apps": {"grokbuild": True},
        }
    }
    apply_command = build_mcp_apply_shell(servers, ["grokbuild"])
    assert apply_command is not None
    applied = subprocess.run(
        ["bash", "-lc", apply_command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert applied.returncode == 0, applied.stderr
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["models"]["default"] == "main"
    assert set(parsed["mcp_servers"]) == {"keep", "remote"}
    assert "type" not in parsed["mcp_servers"]["remote"]
    assert parsed["mcp_servers"]["remote"]["headers"] == {"X-Test": "fixture"}

    scan_command = build_mcp_scan_shell(["grokbuild"])
    scanned = subprocess.run(
        ["bash", "-lc", scan_command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert scanned.returncode == 0, scanned.stderr
    inventory = parse_mcp_scan_output(scanned.stdout)
    assert inventory["grokbuild"]["remote"] == {
        "type": "http",
        "url": "https://mcp.example.test",
        "headers": {"X-Test": "fixture"},
    }

    remove_command = build_mcp_remove_shell({"remote"}, ["grokbuild"])
    assert remove_command is not None
    removed = subprocess.run(
        ["bash", "-lc", remove_command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert removed.returncode == 0, removed.stderr
    after = tomllib.loads(path.read_text(encoding="utf-8"))
    assert set(after["mcp_servers"]) == {"keep"}
    assert after["models"]["default"] == "main"


def test_generated_hermes_mcp_sync_preserves_per_server_fields(tmp_path: Path) -> None:
    import os
    import subprocess
    import yaml

    from app.services.agent_mcp import build_mcp_apply_shell

    path = tmp_path / ".hermes" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        "mcp_servers:\n"
        "  demo:\n"
        "    command: old\n"
        "    enabled: false\n"
        "    timeout: 60\n"
        "    tools: [read]\n"
        "    auth: oauth\n",
        encoding="utf-8",
    )
    command = build_mcp_apply_shell(
        {
            "demo": {
                "id": "demo",
                "spec": {"type": "stdio", "command": "new", "args": ["serve"]},
                "apps": {"hermes": True},
            }
        },
        ["hermes"],
    )
    assert command is not None
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    demo = yaml.safe_load(path.read_text(encoding="utf-8"))["mcp_servers"]["demo"]
    assert demo == {
        "enabled": False,
        "timeout": 60,
        "tools": ["read"],
        "auth": "oauth",
        "command": "new",
        "args": ["serve"],
    }


def test_provider_import_reads_grokbuild_and_hermes_native_configs(tmp_path: Path) -> None:
    from app.services.agent_providers import import_providers_from_home

    grok_path = tmp_path / ".grok" / "config.toml"
    grok_path.parent.mkdir(parents=True)
    grok_path.write_text(
        '[models]\ndefault = "relay"\n\n'
        '[model.relay]\nmodel = "grok-upstream"\nbase_url = "https://grok.example/v1"\n'
        'name = "Grok Relay"\nenv_key = "GROK_RELAY_KEY"\napi_backend = "responses"\n'
        'context_window = 500000\n\n[mcp_servers.keep]\ncommand = "keep"\n',
        encoding="utf-8",
    )
    hermes_path = tmp_path / ".hermes" / "config.yaml"
    hermes_path.parent.mkdir(parents=True)
    hermes_path.write_text(
        "model:\n"
        "  provider: relay-one\n"
        "  default: model-a\n"
        "custom_providers:\n"
        "  - name: relay-one\n"
        "    base_url: https://hermes-one.example/v1\n"
        "    api_key: fixture-one\n"
        "    api_mode: chat_completions\n"
        "    model: model-a\n"
        "    models:\n"
        "      model-a:\n"
        "        context_length: 128000\n"
        "    request_timeout_seconds: 45\n"
        "providers:\n"
        "  overlay:\n"
        "    name: overlay-provider\n"
        "    base_url: https://overlay.example/v1\n"
        "    api_mode: codex_responses\n"
        "    model: model-b\n",
        encoding="utf-8",
    )

    providers = import_providers_from_home(
        tmp_path, apps=["grokbuild", "hermes"]
    )
    by_key = {(item["app_id"], item["provider_id"]): item for item in providers}

    grok = by_key[("grokbuild", "local-current")]
    assert grok["settings"]["routing"] == {
        "base_url": "https://grok.example/v1",
        "api_format": "openai_responses",
        "model": "grok-upstream",
    }
    assert grok["settings"]["grok_profile"] == "relay"
    assert "[mcp_servers.keep]" in grok["settings"]["config"]

    hermes = by_key[("hermes", "relay-one")]
    assert hermes["is_current"] is True
    assert hermes["settings"]["native_source"] == "custom_providers"
    assert hermes["settings"]["native_provider"]["request_timeout_seconds"] == 45
    assert hermes["settings"]["routing"] == {
        "base_url": "https://hermes-one.example/v1",
        "api_format": "openai_chat",
        "api_key": "fixture-one",
        "model": "model-a",
    }

    overlay = by_key[("hermes", "overlay-provider")]
    assert overlay["settings"]["native_source"] == "providers_dict"
    assert overlay["settings"]["native_read_only"] is True
    assert overlay["settings"]["routing"]["api_format"] == "openai_responses"


def test_provider_import_keeps_grokbuild_official_mode_snapshot(tmp_path: Path) -> None:
    from app.services.agent_providers import import_providers_from_home

    path = tmp_path / ".grok" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('[mcp_servers.keep]\ncommand = "keep"\n', encoding="utf-8")

    providers = import_providers_from_home(tmp_path, apps=["grokbuild"])

    assert len(providers) == 1
    settings = providers[0]["settings"]
    assert settings["official_mode"] is True
    assert settings["config"] == '[mcp_servers.keep]\ncommand = "keep"\n'
    assert "routing" not in settings


def test_provider_apply_updates_grokbuild_and_hermes_without_clobbering(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    import tomllib
    import yaml

    from app.services.agent_providers import build_provider_apply_shell

    grok_path = tmp_path / ".grok" / "config.toml"
    grok_path.parent.mkdir(parents=True)
    grok_path.write_text(
        '[models]\ndefault = "old"\n\n'
        '[model.old]\nmodel = "old-model"\nbase_url = "https://old.example/v1"\n'
        'name = "Old"\nenv_key = "OLD_KEY"\napi_backend = "responses"\n'
        'context_window = 100000\n\n[mcp_servers.keep]\ncommand = "keep"\n',
        encoding="utf-8",
    )
    grok_provider = {
        "id": "relay-new",
        "app_id": "grokbuild",
        "name": "Relay New",
        "settings_config": {
            "type": "grokbuild",
            "routing": {
                "base_url": "https://new.example/v1",
                "api_format": "openai_responses",
                "model": "new-model",
            },
        },
    }
    grok_command = build_provider_apply_shell(grok_provider, write_secrets=False)
    assert grok_command is not None
    grok_result = subprocess.run(
        ["bash", "-lc", grok_command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert grok_result.returncode == 0, grok_result.stderr
    grok = tomllib.loads(grok_path.read_text(encoding="utf-8"))
    assert grok["models"]["default"] == "relay_new"
    assert grok["model"]["relay_new"]["model"] == "new-model"
    assert grok["model"]["relay_new"]["base_url"] == "https://new.example/v1"
    assert grok["model"]["relay_new"]["env_key"] == "XAI_API_KEY"
    assert grok["mcp_servers"]["keep"]["command"] == "keep"
    assert grok["model"]["old"]["model"] == "old-model"

    hermes_path = tmp_path / ".hermes" / "config.yaml"
    hermes_path.parent.mkdir(parents=True, exist_ok=True)
    hermes_path.write_text(
        "model:\n"
        "  provider: sibling\n"
        "  default: sibling-model\n"
        "  reasoning_effort: high\n"
        "custom_providers:\n"
        "  - name: sibling\n"
        "    base_url: https://sibling.example/v1\n"
        "    model: sibling-model\n"
        "    future_option: keep\n"
        "mcp_servers:\n"
        "  keep:\n"
        "    command: keep\n",
        encoding="utf-8",
    )
    hermes_provider = {
        "id": "relay-new",
        "app_id": "hermes",
        "name": "Relay New",
        "settings_config": {
            "type": "hermes",
            "routing": {
                "base_url": "https://hermes-new.example/v1",
                "api_format": "openai_responses",
                "model": "new-model",
            },
        },
    }
    hermes_command = build_provider_apply_shell(hermes_provider, write_secrets=False)
    assert hermes_command is not None
    hermes_result = subprocess.run(
        ["bash", "-lc", hermes_command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert hermes_result.returncode == 0, hermes_result.stderr
    hermes = yaml.safe_load(hermes_path.read_text(encoding="utf-8"))
    providers = {item["name"]: item for item in hermes["custom_providers"]}
    assert providers["sibling"]["future_option"] == "keep"
    assert providers["relay-new"]["base_url"] == "https://hermes-new.example/v1"
    assert providers["relay-new"]["api_mode"] == "codex_responses"
    assert providers["relay-new"]["model"] == "new-model"
    assert hermes["model"]["provider"] == "relay-new"
    assert hermes["model"]["default"] == "new-model"
    assert hermes["model"]["reasoning_effort"] == "high"
    assert hermes["mcp_servers"]["keep"]["command"] == "keep"


def test_grokbuild_and_hermes_local_skill_prompt_mcp_api_targets_are_native(
    tmp_path: Path, monkeypatch
) -> None:
    import yaml

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    source = tmp_path / "source-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# Native fixture\n", encoding="utf-8")

    from app.services.agent_mcp import AgentMcpStore
    from app.services.agent_prompts import AgentPromptFileManager
    from app.services.agent_skills import install_skill_to_home, uninstall_skill_from_home

    for client_id, skill_root, prompt_path in (
        ("grokbuild", home / ".grok/skills", home / ".grok/AGENTS.md"),
        ("hermes", home / ".hermes/skills", home / ".hermes/AGENTS.md"),
    ):
        installed = install_skill_to_home(
            home, client_id, "native-demo", str(source), mode="copy"
        )
        assert installed["verified"] is True
        assert (skill_root / "native-demo/SKILL.md").is_file()

        manager = AgentPromptFileManager(home, tmp_path / "prompt-state" / client_id)
        applied = manager.apply(client_id, f"prompt for {client_id}\n")
        assert applied["verified"] is True
        assert prompt_path.read_text(encoding="utf-8") == f"prompt for {client_id}\n"
        assert manager.restore(client_id)["verified"] is True

        removed = uninstall_skill_from_home(home, client_id, "native-demo")
        assert removed["verified"] is True
        assert not (skill_root / "native-demo").exists()

    mcp_store = AgentMcpStore(tmp_path / "data" / "agent")
    mcp_store.upsert_server(
        "native-demo",
        {"command": "node", "args": ["server.js"]},
        {"grokbuild": True, "hermes": True},
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    for client_id, config_path in (
        ("grokbuild", home / ".grok/config.toml"),
        ("hermes", home / ".hermes/config.yaml"),
    ):
        installed = client.post(
            "/api/agent/mcp/local/install",
            json={"client_id": client_id, "mcp_ids": ["native-demo"]},
        )
        assert installed.status_code == 200, installed.text
        assert installed.json()["verified"] is True
        if client_id == "grokbuild":
            assert "[mcp_servers.native-demo]" in config_path.read_text(encoding="utf-8")
        else:
            assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["mcp_servers"]["native-demo"]["command"] == "node"

        removed = client.post(
            "/api/agent/mcp/local/uninstall",
            json={"client_id": client_id, "mcp_ids": ["native-demo"]},
        )
        assert removed.status_code == 200, removed.text
        assert removed.json()["verified"] is True


def test_agent_provider_public_metadata_exposes_native_read_only_and_official_mode(
    tmp_path: Path,
) -> None:
    _write_agent_category(tmp_path)
    from app.services.agent_providers import AgentProviderStore, public_provider

    store = AgentProviderStore(tmp_path / "data" / "agent")
    saved = store.upsert_provider(
        app_id="hermes",
        provider_id="overlay",
        name="Overlay",
        settings={
            "type": "hermes",
            "native_source": "providers_dict",
            "native_read_only": True,
            "native_provider": {"name": "Overlay"},
        },
    )
    safe = public_provider(saved)
    assert safe["native_source"] == "providers_dict"
    assert safe["native_read_only"] is True

    grok = store.upsert_provider(
        app_id="grokbuild",
        provider_id="official",
        name="官方配置",
        settings={"type": "grokbuild", "official_mode": True, "config": ""},
    )
    assert public_provider(grok)["official_mode"] is True


def test_local_mcp_install_uses_client_variant_and_tracks_assignment(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config_path = home / ".codex/config.toml"
    config_path.write_text('model = "keep"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    store = AgentMcpStore(tmp_path / "data/agent")
    store.upsert_server("context7", {"command": "base-command"}, {})
    store.state.upsert_mcp_variant(
        "context7", "codex", "linux", {"command": "codex-command"}, source="test"
    )

    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    installed = client.post(
        "/api/agent/mcp/local/install",
        json={"client_id": "codex", "mcp_ids": ["context7"]},
    )

    assert installed.status_code == 200
    assert 'command = "codex-command"' in config_path.read_text(encoding="utf-8")
    assignments = store.state.list_mcp_assignments("__local__", "codex")
    assert assignments[0]["mcp_id"] == "context7"
    assert assignments[0]["variant_client_id"] == "codex"
    assert assignments[0]["variant_platform"] == "linux"

    removed = client.post(
        "/api/agent/mcp/local/uninstall",
        json={"client_id": "codex", "mcp_ids": ["context7"]},
    )

    assert removed.status_code == 200
    assert store.state.list_mcp_assignments("__local__", "codex") == []
    assert store.get_server("context7") is not None


def test_provider_snapshot_apply_without_secrets_preserves_existing_native_credentials(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from app.services.agent_providers import build_provider_apply_shell

    cases = {
        "claude": (
            tmp_path / ".claude.json",
            {"env": {"ANTHROPIC_API_KEY": "keep-claude", "OLD": "1"}},
            {"config": {"env": {"ANTHROPIC_BASE_URL": "https://relay.example"}}},
            lambda value: value["env"]["ANTHROPIC_API_KEY"],
        ),
        "opencode": (
            tmp_path / ".config/opencode/opencode.json",
            {"options": {"apiKey": "keep-opencode", "old": True}},
            {"config": {"options": {"baseURL": "https://relay.example/v1"}}},
            lambda value: value["options"]["apiKey"],
        ),
        "openclaw": (
            tmp_path / ".openclaw/openclaw.json",
            {"apiKey": "keep-openclaw", "old": True},
            {"config": {"baseUrl": "https://relay.example/v1"}},
            lambda value: value["apiKey"],
        ),
    }

    for app_id, (path, existing, settings, secret_value) in cases.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(existing), encoding="utf-8")
        command = build_provider_apply_shell(
            {"id": "snapshot", "app_id": app_id, "settings_config": settings},
            write_secrets=False,
        )
        assert command is not None
        result = subprocess.run(
            ["bash", "-lc", command],
            env={**os.environ, "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert secret_value(json.loads(path.read_text(encoding="utf-8"))).startswith("keep-")


def test_mcp_api_rejects_clients_without_mcp_projection_support(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw/openclaw.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(
        create_app(config_root=tmp_path, node_scanner=lambda: []),
        raise_server_exceptions=False,
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/agent/mcp/scan",
        json={"node_ids": ["__local__"], "apps": ["openclaw"]},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "node_ids and apps are required"


def test_generated_mcp_scripts_honor_client_specific_home_overrides(
    tmp_path: Path,
) -> None:
    """远程/队列脚本也必须写入各客户端自己的配置目录。"""
    import os
    import subprocess
    import yaml

    from app.services.agent_mcp import (
        build_mcp_apply_shell,
        build_mcp_remove_shell,
        build_mcp_scan_shell,
        parse_mcp_scan_output,
    )

    codex_root = tmp_path / "profiles" / "codex"
    hermes_root = tmp_path / "profiles" / "hermes"
    (codex_root).mkdir(parents=True)
    (codex_root / "config.toml").write_text(
        '[mcp_servers.old]\ncommand = "old"\n', encoding="utf-8"
    )
    (hermes_root).mkdir(parents=True)
    (hermes_root / "config.yaml").write_text("model: keep\n", encoding="utf-8")
    payload = {
        "new": {
            "id": "new",
            "spec": {"type": "stdio", "command": "npx", "args": ["new"]},
            "apps": {},
        }
    }
    apply = build_mcp_apply_shell(payload, ["codex", "hermes"])
    assert apply is not None
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(codex_root),
        "HERMES_HOME": str(hermes_root),
    }
    result = subprocess.run(
        ["bash", "-lc", apply], capture_output=True, text=True, env=env, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "[mcp_servers.new]" in (codex_root / "config.toml").read_text()
    assert not (tmp_path / "home/.codex/config.toml").exists()
    assert yaml.safe_load((hermes_root / "config.yaml").read_text())["mcp_servers"]["new"]["command"] == "npx"

    scan = build_mcp_scan_shell(["codex", "hermes"])
    scanned = subprocess.run(
        ["bash", "-lc", scan], capture_output=True, text=True, env=env, timeout=10
    )
    assert scanned.returncode == 0, scanned.stderr
    inventory = parse_mcp_scan_output(scanned.stdout)
    assert set(inventory["codex"]) == {"old", "new"}
    assert inventory["hermes"]["new"]["command"] == "npx"

    remove = build_mcp_remove_shell({"new"}, ["codex", "hermes"])
    assert remove is not None
    removed = subprocess.run(
        ["bash", "-lc", remove], capture_output=True, text=True, env=env, timeout=10
    )
    assert removed.returncode == 0, removed.stderr
    assert "new" not in (codex_root / "config.toml").read_text()
    assert "new" not in yaml.safe_load((hermes_root / "config.yaml").read_text())["mcp_servers"]


def test_import_mcp_uses_explicit_client_home_override(tmp_path: Path) -> None:
    from app.services.agent_mcp import import_mcp_from_home

    custom_root = tmp_path / "custom-codex"
    custom_root.mkdir(parents=True)
    (custom_root / "config.toml").write_text(
        '[mcp_servers.custom]\ncommand = "custom"\n', encoding="utf-8"
    )

    imported = import_mcp_from_home(
        tmp_path / "empty-home",
        apps=["codex"],
        environ={"CODEX_HOME": str(custom_root)},
    )

    assert imported["custom"]["apps"] == {"codex": True}


def test_provider_snapshot_apply_without_secrets_protects_grok_and_hermes_credentials(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    import tomllib
    import yaml

    from app.services.agent_providers import build_provider_apply_shell

    grok_path = tmp_path / ".grok" / "config.toml"
    grok_path.parent.mkdir(parents=True)
    grok_path.write_text(
        '[models]\ndefault = "relay"\n\n'
        '[model.relay]\nmodel = "old-model"\nbase_url = "https://old.example/v1"\n'
        'api_key = "keep-grok"\nname = "Old"\n\n'
        '[model.sibling]\nmodel = "sibling"\nbase_url = "https://sibling.example/v1"\n\n'
        '[mcp_servers.keep]\ncommand = "keep"\n',
        encoding="utf-8",
    )
    hermes_path = tmp_path / ".hermes" / "config.yaml"
    hermes_path.parent.mkdir(parents=True)
    hermes_path.write_text(
        "custom_providers:\n"
        "  - name: relay\n"
        "    base_url: https://old-hermes.example/v1\n"
        "    api_key: keep-hermes\n"
        "    model: old-model\n"
        "    future: keep\n"
        "  - name: sibling\n"
        "    base_url: https://sibling.example/v1\n"
        "mcp_servers:\n  keep:\n    command: keep\n",
        encoding="utf-8",
    )
    env = {**os.environ, "HOME": str(tmp_path)}

    for app_id, settings in (
        (
            "grokbuild",
            {
                "config":
                '[models]\ndefault = "relay"\n\n'
                '[model.relay]\nmodel = "new-model"\nbase_url = "https://new.example/v1"\n'
                'api_key = "incoming-grok-secret"\nname = "New"\n\n'
                '[model.incoming]\nmodel = "incoming"\napi_key = "new-secret"\n',
            },
        ),
        (
            "hermes",
            {
                "config":
                "custom_providers:\n"
                "  - name: relay\n"
                "    base_url: https://new-hermes.example/v1\n"
                "    api_key: incoming-hermes-secret\n"
                "    model: new-model\n"
                "    future: changed\n"
                "  - name: incoming\n"
                "    base_url: https://incoming.example/v1\n"
                "    api_key: new-secret\n"
                "mcp_servers:\n  keep:\n    command: keep\n",
            },
        ),
    ):
        command = build_provider_apply_shell(
            {"id": "snapshot", "app_id": app_id, "settings_config": settings},
            write_secrets=False,
        )
        assert command is not None
        result = subprocess.run(
            ["bash", "-lc", command], env=env, capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 0, result.stderr

    grok = tomllib.loads(grok_path.read_text())
    assert grok["model"]["relay"]["api_key"] == "keep-grok"
    assert "api_key" not in grok["model"].get("incoming", {})
    assert grok["mcp_servers"]["keep"]["command"] == "keep"
    hermes = yaml.safe_load(hermes_path.read_text())
    providers = {item["name"]: item for item in hermes["custom_providers"]}
    assert providers["relay"]["api_key"] == "keep-hermes"
    assert "api_key" not in providers["incoming"]
    assert hermes["mcp_servers"]["keep"]["command"] == "keep"


def test_generated_mcp_apply_validates_existing_json_and_uses_atomic_readback(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from app.services.agent_mcp import build_mcp_apply_shell

    path = tmp_path / ".gemini" / "settings.json"
    path.parent.mkdir(parents=True)
    original = '{not-json\n'
    path.write_text(original, encoding="utf-8")
    command = build_mcp_apply_shell(
        {"demo": {"id": "demo", "spec": {"command": "node"}, "apps": {}}},
        ["gemini"],
    )
    assert command is not None
    assert "os.replace" in command
    result = subprocess.run(
        ["bash", "-lc", command],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode != 0
    assert path.read_text(encoding="utf-8") == original


def test_agent_route_panel_renders_current_client_failover_policy(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_router_config import AgentRouterConfigStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/nonexistent")
    AgentRouterConfigStore(tmp_path / "data/agent").set_provider(
        "codex",
        {
            "base_url": "https://relay.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
            "auto_failover": True,
            "max_retries": 0,
            "failure_threshold": 2,
            "cooldown_seconds": 45,
        },
        provider_id="relay",
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-router-configured="true"' in response.text
    assert 'data-agent-router-auto-failover="true"' in response.text
    assert 'data-agent-router-max-retries="0"' in response.text
    assert 'data-agent-router-failure-threshold="2"' in response.text
    assert 'data-agent-router-cooldown-seconds="45"' in response.text
    assert "data-agent-router-policy" in response.text
    assert "data-agent-router-policy-save" in response.text


def test_current_provider_delete_is_rejected_and_noncurrent_delete_prunes_router(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore

    _write_agent_category(tmp_path)
    data_root = tmp_path / "data/agent"
    providers = AgentProviderStore(data_root)
    for provider_id, current in (("primary", True), ("backup", False)):
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
            is_current=current,
        )
    router = AgentRouterConfigStore(data_root)
    router.set_provider(
        "codex",
        {
            "base_url": "https://primary.example/v1",
            "api_format": "openai_responses",
            "auth_mode": "none",
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
    router.set_failover_queue("codex", ["primary", "backup"])
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    rejected = client.delete("/api/agent/providers/codex/primary")
    deleted = client.delete("/api/agent/providers/codex/backup")

    assert rejected.status_code == 409, rejected.text
    assert providers.get_provider("codex", "primary") is not None
    assert deleted.status_code == 200, deleted.text
    assert providers.get_provider("codex", "backup") is None
    snapshot = router.snapshot()
    assert snapshot["failover_queues"]["codex"] == ["primary"]
    assert snapshot["providers"]["codex"].get("fallbacks") == []


def test_agent_provider_import_is_locked_but_hot_switch_stays_available_under_takeover(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/nonexistent")
    data_root = tmp_path / "data/agent"
    AgentRouterConfigStore(data_root).set_takeover("codex", True)
    AgentProviderStore(data_root).upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "config": (
                'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                'wire_api = "responses"\nrequires_openai_auth = false\n'
            )
        },
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-router-takeover="true"' in response.text
    assert 'data-agent-provider-import disabled' in response.text
    assert 'data-agent-provider-apply' not in response.text
    assert 'data-agent-provider-activate="codex::relay"' in response.text
    assert 'data-agent-provider-activate="codex::relay" disabled' not in response.text
    assert '>切换路由</button>' in response.text
    assert 'title="Router 接管中，先关闭当前客户端接管"' in response.text


def test_agent_provider_actions_use_real_takeover_record_when_flag_is_stale(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_route_takeover import AgentRouteTakeover
    from app.services.agent_router_config import AgentRouterConfigStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    config = home / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model_provider = "original"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/nonexistent")
    data_root = tmp_path / "data/agent"
    takeover = AgentRouteTakeover(home, data_root / "takeover")
    takeover.enable("codex", "http://127.0.0.1:7888/codex/v1")
    assert AgentRouterConfigStore(data_root).snapshot().get("takeover") == {}
    AgentProviderStore(data_root).upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "config": (
                'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                'wire_api = "responses"\nrequires_openai_auth = false\n'
            )
        },
    )

    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-router-takeover="true"' in response.text
    assert 'data-agent-provider-import disabled' in response.text
    assert 'data-agent-provider-apply' not in response.text
    assert 'data-agent-provider-activate="codex::relay"' in response.text
    assert 'data-agent-provider-activate="codex::relay" disabled' not in response.text
    assert '>切换路由</button>' in response.text


def test_workbench_library_preserves_database_resource_order(tmp_path: Path) -> None:
    from app.services.agent_workbench import build_agent_workbench_context
    from app.services.state_store import PanelStateStore

    state = PanelStateStore(tmp_path / "data/agent")
    for resource_id in ("zeta", "alpha"):
        state.upsert_mcp_server(resource_id, {"command": resource_id})
        state.upsert_skill(
            skill_id=resource_id,
            name=resource_id.title(),
            source_kind="local",
            ssot_path=str(tmp_path / "skills" / resource_id),
            content_hash=f"hash-{resource_id}",
        )
        state.upsert_prompt(resource_id, resource_id.title(), f"{resource_id}\n")
        state.upsert_profile(resource_id, resource_id.title(), None, [])
        state.upsert_agent_provider(
            app_id="codex",
            provider_id=resource_id,
            name=resource_id.title(),
            settings={"type": "codex"},
        )

    library = build_agent_workbench_context(
        tmp_path,
        home=tmp_path / "home",
        which=lambda _name: None,
    )["agent_library"]

    assert [item["id"] for item in library["mcp"]] == ["zeta", "alpha"]
    assert [item["id"] for item in library["skills"]] == ["zeta", "alpha"]
    assert [item["id"] for item in library["prompts"]] == ["zeta", "alpha"]
    assert [item["id"] for item in library["profiles"]] == ["zeta", "alpha"]
    assert [
        item["id"] for item in library["providers"] if item["app_id"] == "codex"
    ] == ["zeta", "alpha"]


def test_provider_detail_exposes_native_summary_form_and_meta_without_secret(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import (
        AgentProviderStore,
        REDACTED_SECRET,
        public_provider,
        redact_sensitive,
    )

    data_root = tmp_path / "data" / "agent"
    store = AgentProviderStore(data_root)
    stored = store.upsert_provider(
        app_id="openclaw",
        provider_id="relay",
        name="Relay",
        settings={
            "baseUrl": "https://claw.example/v1",
            "apiKey": "private-key",
            "api": "openai-responses",
            "models": [{"id": "gpt-5.6"}],
        },
        meta={"model_map": {"gpt-5.6": "upstream"}},
    )

    listed = public_provider(stored)
    detailed = public_provider(stored, include_settings=True)

    assert "settings_config" not in listed
    assert listed["summary"]["base_url"] == "https://claw.example/v1"
    assert listed["summary"]["model"] == "gpt-5.6"
    assert listed["summary"]["has_credentials"] is True
    assert listed["meta"] == {"model_map": {"gpt-5.6": "upstream"}}
    assert detailed["form"]["base_url"] == "https://claw.example/v1"
    assert detailed["form"]["api_key"] == REDACTED_SECRET
    assert detailed["form"]["auth_mode"] == "bearer"
    assert detailed["settings_config"]["apiKey"] == REDACTED_SECRET
    assert redact_sensitive(
        {"authMode": "bearer", "clientSecret": "private-client-secret"}
    ) == {"authMode": "bearer", "clientSecret": REDACTED_SECRET}
    assert "private-key" not in json.dumps(detailed, ensure_ascii=False)


def test_provider_import_uses_native_exclusive_and_additive_semantics(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import AgentProviderStore

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude/settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://claude.example/v1",
                    "ANTHROPIC_AUTH_TOKEN": "claude-key",
                    "ANTHROPIC_MODEL": "claude-main",
                },
                "hooks": {"keep": True},
            }
        ),
        encoding="utf-8",
    )
    (home / ".openclaw").mkdir()
    openclaw_path = home / ".openclaw/openclaw.json"
    openclaw_path.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "one": {"baseUrl": "https://one", "models": [{"id": "m1"}]},
                        "two": {"baseUrl": "https://two", "models": [{"id": "m2"}]},
                    }
                },
                "agents": {"defaults": {"model": {"primary": "two/m2"}}},
            }
        ),
        encoding="utf-8",
    )
    store = AgentProviderStore(tmp_path / "data" / "agent")

    assert store.import_from_home(home, apps=["claude"]) == 1
    claude = store.get_provider("claude", "default")
    assert claude is not None
    assert claude["settings_config"]["hooks"] == {"keep": True}
    assert claude["summary"]["base_url"] == "https://claude.example/v1"

    store.upsert_provider(
        app_id="claude",
        provider_id="manual",
        name="Manual",
        settings={"env": {"ANTHROPIC_BASE_URL": "https://manual.example"}},
        source="manual",
    )
    (home / ".claude/settings.json").write_text(
        json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://must-not-overwrite.example"}}),
        encoding="utf-8",
    )
    assert store.import_from_home(home, apps=["claude"]) == 0
    assert store.get_provider("claude", "default")["summary"]["base_url"] == (
        "https://claude.example/v1"
    )

    assert store.import_from_home(home, apps=["openclaw"]) == 2
    assert set(store.list_providers("openclaw")["openclaw"][0]) >= {"id", "summary"}
    assert {item["id"] for item in store.list_providers("openclaw")["openclaw"]} == {
        "one",
        "two",
    }
    store.upsert_provider(
        app_id="openclaw",
        provider_id="one",
        name="One edited",
        settings={"baseUrl": "https://manual-one", "models": [{"id": "m1"}]},
        source="manual",
    )
    payload = json.loads(openclaw_path.read_text())
    payload["models"]["providers"]["one"]["baseUrl"] = "https://live-one"
    payload["models"]["providers"]["two"]["baseUrl"] = "https://live-two"
    openclaw_path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.import_from_home(home, apps=["openclaw"]) == 1
    assert store.get_provider("openclaw", "one")["settings_config"]["baseUrl"] == (
        "https://manual-one"
    )
    assert store.get_provider("openclaw", "two")["settings_config"]["baseUrl"] == (
        "https://live-two"
    )


def test_provider_api_saves_client_form_as_native_settings(
    tmp_path: Path, monkeypatch
) -> None:
    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    saved = client.post(
        "/api/agent/providers",
        json={
            "app_id": "codex",
            "provider_id": "relay",
            "name": "Relay",
            "notes": "Codex relay",
            "form": {
                "provider_key": "relay",
                "base_url": "https://relay.example/v1",
                "api_key": "private-key",
                "model": "gpt-5.6",
                "api_format": "openai_responses",
                "auth_mode": "bearer",
            },
            "meta": {
                "model_map": {"gpt-5.6": "upstream"},
                "use_outbound_proxy": False,
            },
        },
    )
    detail = client.get("/api/agent/providers/codex/relay")

    assert saved.status_code == 200, saved.text
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["summary"]["base_url"] == "https://relay.example/v1"
    assert body["summary"]["model"] == "gpt-5.6"
    assert body["form"]["base_url"] == "https://relay.example/v1"
    assert body["form"]["api_key"] == "••••••••"
    assert body["meta"]["model_map"] == {"gpt-5.6": "upstream"}
    assert body["meta"]["api_format"] == "openai_responses"
    assert body["meta"]["auth_mode"] == "bearer"
    assert "private-key" not in detail.text
    assert "settings_config" in body



def test_provider_api_edit_preserves_current_when_status_is_omitted(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    store = AgentProviderStore(tmp_path / "data/agent")
    store.upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "auth": {"OPENAI_API_KEY": "private-key"},
            "config": (
                'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                'wire_api = "responses"\n'
            ),
        },
        is_current=True,
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/agent/providers",
        json={
            "app_id": "codex",
            "provider_id": "relay",
            "name": "Relay edited",
            "notes": "只修改数据库描述",
            "form": {
                "provider_key": "relay",
                "base_url": "https://relay.example/v1",
                "api_key": "••••••••",
                "model": "gpt-5.6",
                "api_format": "openai_responses",
                "auth_mode": "bearer",
            },
        },
    )

    assert response.status_code == 200, response.text
    assert store.get_provider("codex", "relay")["is_current"] is True


def test_provider_duplicate_copies_native_meta_description_and_order(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import AgentProviderStore

    _write_agent_category(tmp_path)
    store = AgentProviderStore(tmp_path / "data/agent")
    store.upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "auth": {"OPENAI_API_KEY": "private-key"},
            "config": (
                'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                'wire_api = "responses"\n'
            ),
        },
        meta={"model_map": {"gpt-5.6": "upstream"}},
        notes="original note",
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post("/api/agent/providers/codex/relay/duplicate", json={})

    assert response.status_code == 200, response.text
    duplicate = store.get_provider("codex", "relay-copy")
    assert duplicate is not None
    assert duplicate["name"] == "Relay 副本"
    assert duplicate["notes"] == "original note"
    assert duplicate["settings_config"] == store.get_provider("codex", "relay")["settings_config"]
    assert duplicate["meta"] == {"model_map": {"gpt-5.6": "upstream"}}
    assert [item["id"] for item in store.list_providers("codex")["codex"]] == [
        "relay",
        "relay-copy",
    ]
    assert "private-key" not in response.text


def test_provider_activate_under_router_takeover_only_hot_switches_router(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore
    from app.tasks.store import InMemoryTaskStore

    _write_agent_category(tmp_path)
    providers = AgentProviderStore(tmp_path / "data/agent")
    providers.upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "auth": {"OPENAI_API_KEY": "private-key"},
            "config": (
                'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
                'wire_api = "responses"\n'
            ),
        },
    )
    router = AgentRouterConfigStore(tmp_path / "data/agent")
    router.set_takeover("codex", True)
    tasks = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=tasks, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post("/api/agent/providers/codex/relay/activate", json={})

    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "router"
    assert response.json()["queued_count"] == 0
    assert tasks.list_all() == []
    assert router.snapshot()["provider_ids"]["codex"] == "relay"
    assert router.snapshot()["providers"]["codex"]["base_url"] == (
        "https://relay.example/v1"
    )
    assert providers.get_provider("codex", "relay")["is_current"] is True
    assert "private-key" not in response.text


def test_provider_activate_direct_queues_native_write_and_sets_current(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.tasks.store import InMemoryTaskStore

    _write_agent_category(tmp_path)
    providers = AgentProviderStore(tmp_path / "data/agent")
    providers.upsert_provider(
        app_id="codex",
        provider_id="one",
        name="One",
        settings={"auth": {}, "config": 'model = "one"\n'},
        is_current=True,
    )
    providers.upsert_provider(
        app_id="codex",
        provider_id="two",
        name="Two",
        settings={"auth": {}, "config": 'model = "two"\n'},
    )
    tasks = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=tasks, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post("/api/agent/providers/codex/two/activate", json={})

    assert response.status_code == 202, response.text
    assert response.json()["mode"] == "direct"
    assert response.json()["queued_count"] == 1
    assert tasks.list_all()[0].action == "agent_provider_activate"
    assert providers.get_provider("codex", "one")["is_current"] is False
    assert providers.get_provider("codex", "two")["is_current"] is True


def test_additive_provider_apply_and_remove_shell_preserve_siblings(
    tmp_path: Path,
) -> None:
    import os
    import subprocess
    import yaml

    from app.services.agent_providers import (
        build_provider_apply_shell,
        build_provider_remove_shell,
    )

    opencode_path = tmp_path / ".config/opencode/opencode.json"
    opencode_path.parent.mkdir(parents=True)
    opencode_path.write_text(
        json.dumps(
            {
                "theme": "keep",
                "provider": {"sibling": {"npm": "keep", "options": {}}},
            }
        ),
        encoding="utf-8",
    )
    openclaw_path = tmp_path / ".openclaw/openclaw.json"
    openclaw_path.parent.mkdir(parents=True)
    openclaw_path.write_text(
        json.dumps(
            {
                "gateway": {"keep": True},
                "models": {"providers": {"sibling": {"baseUrl": "https://keep"}}},
            }
        ),
        encoding="utf-8",
    )
    hermes_path = tmp_path / ".hermes/config.yaml"
    hermes_path.parent.mkdir(parents=True)
    hermes_path.write_text(
        "agent:\n  keep: true\ncustom_providers:\n"
        "  - name: sibling\n    base_url: https://keep\n",
        encoding="utf-8",
    )
    fixtures = {
        "opencode": {
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "https://relay.example/v1", "apiKey": "secret"},
            "models": {"gpt-5": {"name": "GPT-5"}},
        },
        "openclaw": {
            "baseUrl": "https://relay.example/v1",
            "apiKey": "secret",
            "api": "openai-responses",
            "models": [{"id": "gpt-5"}],
        },
        "hermes": {
            "name": "relay",
            "base_url": "https://relay.example/v1",
            "api_key": "secret",
            "api": "openai-chat",
            "models": {"gpt-5": {}},
        },
    }
    env = {**os.environ, "HOME": str(tmp_path)}

    for app_id, settings in fixtures.items():
        apply_command = build_provider_apply_shell(
            {
                "id": "relay",
                "app_id": app_id,
                "name": "Relay",
                "settings_config": settings,
            },
            write_secrets=True,
        )
        assert apply_command is not None
        applied = subprocess.run(
            ["bash", "-lc", apply_command],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert applied.returncode == 0, f"{app_id}: {applied.stderr}"
        remove_command = build_provider_remove_shell(app_id, "relay")
        assert remove_command is not None
        removed = subprocess.run(
            ["bash", "-lc", remove_command],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert removed.returncode == 0, f"{app_id}: {removed.stderr}"

    opencode = json.loads(opencode_path.read_text())
    assert opencode["theme"] == "keep"
    assert list(opencode["provider"]) == ["sibling"]
    openclaw = json.loads(openclaw_path.read_text())
    assert openclaw["gateway"] == {"keep": True}
    assert list(openclaw["models"]["providers"]) == ["sibling"]
    hermes = yaml.safe_load(hermes_path.read_text())
    assert hermes["agent"] == {"keep": True}
    assert [item["name"] for item in hermes["custom_providers"]] == ["sibling"]


def test_additive_live_provider_and_readonly_provider_must_be_removed_before_delete(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw/openclaw.json").write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "relay": {"baseUrl": "https://relay", "models": []}
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    store = AgentProviderStore(tmp_path / "data/agent")
    store.upsert_provider(
        app_id="openclaw",
        provider_id="relay",
        name="Relay",
        settings={"baseUrl": "https://relay", "models": []},
    )
    store.upsert_provider(
        app_id="hermes",
        provider_id="built-in",
        name="Built In",
        settings={"base_url": "https://built-in"},
        meta={"native_read_only": True},
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    live = client.delete("/api/agent/providers/openclaw/relay")
    readonly = client.delete("/api/agent/providers/hermes/built-in")

    assert live.status_code == 409, live.text
    assert "先从客户端移除" in live.text
    assert readonly.status_code == 409, readonly.text
    assert "原生只读" in readonly.text


def test_agent_provider_page_renders_native_summary_and_client_specific_editor(
    tmp_path: Path, monkeypatch
) -> None:
    from app.services.agent_providers import AgentProviderStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "gpt-5.6"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/nonexistent")
    AgentProviderStore(tmp_path / "data/agent").upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Team Relay",
        settings={
            "auth": {"OPENAI_API_KEY": "private-key"},
            "config": (
                'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                '[model_providers.relay]\nname = "Relay"\n'
                'base_url = "https://relay.example/v1"\nwire_api = "responses"\n'
            ),
        },
        notes="团队线路",
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    page = client.get("/categories/agent")

    assert page.status_code == 200
    assert 'data-provider-base-url="https://relay.example/v1"' in page.text
    assert '>https://relay.example/v1<' in page.text
    assert 'data-provider-model="gpt-5.6"' in page.text
    assert 'data-provider-format="openai_responses"' in page.text
    assert 'data-provider-auth-state="configured"' in page.text
    assert 'data-agent-provider-fields="codex"' in page.text
    assert "private-key" not in page.text


def test_agent_provider_context_reads_additive_live_state_from_native_config(
    tmp_path: Path,
) -> None:
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_workbench import build_agent_workbench_context

    home = tmp_path / "home"
    config_root = tmp_path / "panel"
    opencode_root = home / ".config" / "opencode"
    opencode_root.mkdir(parents=True)
    (opencode_root / "opencode.json").write_text(
        json.dumps(
            {
                "provider": {
                    "live": {
                        "npm": "@ai-sdk/openai-compatible",
                        "options": {"baseURL": "https://live.example/v1"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    store = AgentProviderStore(config_root / "data" / "agent")
    store.upsert_provider(
        app_id="opencode",
        provider_id="live",
        name="Live",
        settings={
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "https://live.example/v1"},
        },
    )
    store.upsert_provider(
        app_id="opencode",
        provider_id="saved",
        name="Saved",
        settings={
            "npm": "@ai-sdk/openai-compatible",
            "options": {"baseURL": "https://saved.example/v1"},
        },
    )

    context = build_agent_workbench_context(
        config_root, home=home, which=lambda _name: None
    )
    providers = {
        item["id"]: item
        for item in context["agent_providers"]["opencode"]
    }

    assert providers["live"]["live_state"] == "added"
    assert providers["live"]["live_state_known"] is True
    assert providers["saved"]["live_state"] == "saved"
    assert providers["saved"]["live_state_known"] is True
    library = {
        item["id"]: item
        for item in context["agent_library"]["providers"]
        if item["app_id"] == "opencode"
    }
    assert library["live"]["live_state"] == "added"
    assert library["saved"]["live_state"] == "saved"
