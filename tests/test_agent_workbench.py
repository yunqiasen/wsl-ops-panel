import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.services.agent_clients import AGENT_CLIENTS
from app.services.agent_mcp import AgentMcpStore, import_mcp_from_home
from app.services.agent_prompts import AgentPromptStore, build_prompt_apply_shell
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


def test_import_mcp_from_home_respects_selected_clients(tmp_path: Path) -> None:
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

    assert "codex-demo" not in servers
    assert servers["claw-demo"]["apps"]["openclaw"] is True


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
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert "Agent 工作台" in response.text
    assert "agent-shell" in response.text
    assert "agent-editor" in response.text
    assert 'data-agent-tab-control="route"' in response.text
    assert 'data-agent-tab-control="skills"' in response.text
    assert "data-agent-mcp-install" in response.text
    assert "data-agent-app" in response.text
    assert 'value="codex"' in response.text
    assert "data-agent-scope-device" not in response.text
    assert "data-agent-mcp-matrix" not in response.text
    assert "执行设备" not in response.text
    assert "未检测" not in response.text


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
    assert "data-agent-skill-install" in response.text
    assert "data-agent-skill-update" in response.text
    assert "data-agent-skill-delete" in response.text
    assert "data-agent-skill-mode" in response.text
    assert "data-agent-mcp-delete" in response.text
    assert "data-agent-prompt-delete" in response.text
    assert "data-agent-prompt-import-current" in response.text
    assert "data-agent-prompt-restore" in response.text


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
    provider = store.get_provider("codex", "local-current")
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


def test_same_mcp_id_with_different_client_specs_is_not_overwritten(
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

    servers = import_mcp_from_home(home, apps=["codex", "claude"])

    assert servers["demo"]["spec"]["command"] == "node"
    assert servers["demo--claude"]["spec"]["command"] == "uvx"
    assert servers["demo--claude"]["apps"] == {"claude": True}


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
    payload = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
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


def test_mcp_adapters_handle_opencode_openclaw_and_hermes(tmp_path: Path) -> None:
    from app.services.agent_mcp_adapters import apply_mcp_to_home, scan_mcp_home

    apply_mcp_to_home(
        tmp_path,
        "opencode",
        {"demo": {"type": "stdio", "command": "npx", "args": ["demo"]}},
    )
    apply_mcp_to_home(
        tmp_path,
        "openclaw",
        {"demo": {"type": "http", "url": "https://example.test/mcp"}},
    )
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".hermes" / "config.yaml").write_text("model: keep\n", encoding="utf-8")
    apply_mcp_to_home(
        tmp_path, "hermes", {"demo": {"command": "npx", "args": ["demo"]}}
    )

    assert scan_mcp_home(tmp_path, "opencode")["demo"]["command"] == "npx"
    assert (
        scan_mcp_home(tmp_path, "openclaw")["demo"]["url"] == "https://example.test/mcp"
    )
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

    assert rows["context7"]["observations"]["codex"]["status"] == "installed"
    assert rows["context7"]["managed"] is False
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
    assert AgentMcpStore(tmp_path / "data" / "agent").get_server("context7") is not None


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

    for client_id in ("codex", "claude", "gemini", "opencode", "openclaw", "hermes"):
        remove_mcp_from_home(tmp_path, client_id, {"remove"})
        assert set(scan_mcp_home(tmp_path, client_id)) == {"keep"}

    assert 'model = "keep"' in (tmp_path / ".codex" / "config.toml").read_text()
    assert json.loads((tmp_path / ".claude.json").read_text())["theme"] == "keep"
    assert json.loads((tmp_path / ".gemini" / "settings.json").read_text())["theme"] == "keep"
    assert json.loads((tmp_path / ".config" / "opencode" / "opencode.json").read_text())["theme"] == "keep"
    assert json.loads((tmp_path / ".openclaw" / "openclaw.json").read_text())["theme"] == "keep"
    import yaml

    assert yaml.safe_load((tmp_path / ".hermes" / "config.yaml").read_text())["model"] == "keep"


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
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert 'data-agent-mcp-managed="false"' in response.text
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
