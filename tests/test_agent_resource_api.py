from __future__ import annotations

from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.services.agent_mcp import AgentMcpStore
from app.services.agent_skill_store import AgentSkillStore
from app.services.state_store import PanelStateStore


def _write_agent_category(root: Path) -> None:
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "categories/agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "rules/node-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    (root / "rules/python-packages.yaml").write_text("packages: []\n", encoding="utf-8")


def _client(root: Path, monkeypatch, home: Path) -> TestClient:
    _write_agent_category(root)
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=root, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return client


def _skill_source(root: Path, content: str = "# Demo\n") -> Path:
    source = root / "skill-source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text(content, encoding="utf-8")
    return source


def test_skill_library_import_plan_install_and_uninstall(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    source = _skill_source(tmp_path)
    client = _client(tmp_path, monkeypatch, home)

    imported = client.post(
        "/api/agent/skills/import-source",
        json={"skill_id": "demo", "name": "Demo", "source": str(source)},
    )

    assert imported.status_code == 200, imported.text
    assert not (home / ".codex/skills/demo").exists()
    library = client.get("/api/agent/library")
    assert library.status_code == 200
    assert library.json()["skills"][0]["id"] == "demo"

    plan = client.post(
        "/api/agent/resources/plan",
        json={
            "action": "install",
            "resource_type": "skill",
            "resource_ids": ["demo"],
            "client_id": "codex",
            "node_id": "__local__",
        },
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["operations"][0]["action"] == "install"

    installed = client.post(
        "/api/agent/resources/reconcile",
        json={
            "action": "install",
            "resource_type": "skill",
            "resource_ids": ["demo"],
            "client_id": "codex",
        },
    )
    assert installed.status_code == 200, installed.text
    assert installed.json()["verified"] is True
    assert (home / ".codex/skills/demo/SKILL.md").is_file()

    no_change = client.post(
        "/api/agent/resources/plan",
        json={"action": "sync", "client_id": "codex"},
    )
    assert no_change.status_code == 200
    assert no_change.json()["changed"] is False

    removed = client.post(
        "/api/agent/resources/reconcile",
        json={
            "action": "uninstall",
            "resource_type": "skill",
            "resource_ids": ["demo"],
            "client_id": "codex",
        },
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] == ["demo"]
    assert AgentSkillStore(tmp_path / "data/agent").get("demo") is not None


def test_current_client_imports_create_managed_variants_without_rewriting_files(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    mcp_file = home / ".codex/config.toml"
    mcp_file.write_text('[mcp_servers.demo]\ncommand = "node"\n', encoding="utf-8")
    prompt_file = home / ".codex/AGENTS.md"
    prompt_file.write_text("native prompt\n", encoding="utf-8")
    skill_dir = home / ".codex/skills/native"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Native\n", encoding="utf-8")
    client = _client(tmp_path, monkeypatch, home)

    mcp = client.post("/api/agent/mcp/import-current", json={"client_id": "codex"})
    prompt = client.post(
        "/api/agent/prompts/import-current",
        json={"client_id": "codex", "prompt_id": "native", "name": "Native"},
    )
    skill = client.post(
        "/api/agent/skills/import-current",
        json={"client_id": "codex", "skill_name": "native"},
    )

    assert mcp.status_code == 200, mcp.text
    assert prompt.status_code == 200, prompt.text
    assert skill.status_code == 200, skill.text
    library = client.get("/api/agent/library").json()
    assert [item["id"] for item in library["mcp"]] == ["demo"]
    assert [item["id"] for item in library["prompts"]] == ["native"]
    assert [item["id"] for item in library["skills"]] == ["native"]
    assert mcp_file.read_text(encoding="utf-8").startswith("[mcp_servers.demo]")
    assert prompt_file.read_text(encoding="utf-8") == "native prompt\n"


def test_mcp_discovery_import_only_manages_the_selected_row(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config = home / ".codex/config.toml"
    original = (
        '[mcp_servers.selected]\ncommand = "selected"\n'
        '[mcp_servers.untouched]\ncommand = "untouched"\n'
    )
    config.write_text(original, encoding="utf-8")
    client = _client(tmp_path, monkeypatch, home)

    imported = client.post(
        "/api/agent/mcp/import-current",
        json={"client_id": "codex", "mcp_ids": ["selected"]},
    )

    assert imported.status_code == 200, imported.text
    assert imported.json()["imported_count"] == 1
    library = client.get("/api/agent/library").json()
    assert [item["id"] for item in library["mcp"]] == ["selected"]
    assert [item["id"] for item in library["discovery"]["mcp"]] == ["untouched"]
    assignments = PanelStateStore(tmp_path / "data/agent").list_mcp_assignments(
        "__local__", "codex"
    )
    assert [item["mcp_id"] for item in assignments] == ["selected"]
    assert config.read_text(encoding="utf-8") == original


def test_profile_crud_and_apply_uses_minimal_local_resource_plan(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    source = _skill_source(tmp_path)
    client = _client(tmp_path, monkeypatch, home)
    client.post(
        "/api/agent/skills/import-source",
        json={"skill_id": "review", "name": "Review", "source": str(source)},
    )

    saved = client.put(
        "/api/agent/profiles/work",
        json={
            "name": "Work",
            "description": "Codex work setup",
            "items": [
                {
                    "client_id": "codex",
                    "resource_type": "skill",
                    "resource_id": "review",
                }
            ],
        },
    )
    assert saved.status_code == 200, saved.text
    assert client.get("/api/agent/profiles").json()["profiles"][0]["id"] == "work"

    applied = client.post(
        "/api/agent/profiles/work/apply",
        json={"node_id": "__local__", "client_id": "codex"},
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["verified"] is True
    assert (home / ".codex/skills/review/SKILL.md").is_file()

    emptied = client.put(
        "/api/agent/profiles/work",
        json={"name": "Work", "description": "Empty setup", "items": []},
    )
    assert emptied.status_code == 200
    removed = client.post(
        "/api/agent/profiles/work/apply",
        json={"node_id": "__local__", "client_id": "codex"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] == ["review"]
    assert not (home / ".codex/skills/review").exists()
    assert client.delete("/api/agent/profiles/work").json() == {"deleted": True}


def test_delete_from_library_uninstalls_assignments_before_deleting_resource(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    source = _skill_source(tmp_path)
    client = _client(tmp_path, monkeypatch, home)
    client.post(
        "/api/agent/skills/import-source",
        json={"skill_id": "demo", "name": "Demo", "source": str(source)},
    )
    client.post(
        "/api/agent/resources/reconcile",
        json={
            "action": "install",
            "resource_type": "skill",
            "resource_ids": ["demo"],
            "client_id": "codex",
        },
    )

    deleted = client.delete("/api/agent/resources/skill/demo")

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] is True
    assert deleted.json()["uninstalled"][0]["client_id"] == "codex"
    assert not (home / ".codex/skills/demo").exists()
    assert AgentSkillStore(tmp_path / "data/agent").get("demo") is None


def test_delete_from_library_keeps_resource_when_nonlocal_assignment_exists(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    source = _skill_source(tmp_path)
    client = _client(tmp_path, monkeypatch, home)
    store = AgentSkillStore(tmp_path / "data/agent")
    store.import_source("demo", "Demo", str(source))
    store.state.set_skill_assignment("remote-node", "codex", "demo")

    deleted = client.delete("/api/agent/resources/skill/demo")

    assert deleted.status_code == 409
    assert store.get("demo") is not None


def test_library_redacts_mcp_secrets_and_exposes_discovery_separately(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.native]\ncommand = "node"\n', encoding="utf-8"
    )
    store = AgentMcpStore(tmp_path / "data/agent")
    store.upsert_server(
        "managed",
        {"command": "node", "env": {"TOKEN": "top-secret"}},
    )
    store.state.upsert_mcp_variant(
        "managed",
        "codex",
        "linux",
        {"command": "node", "env": {"TOKEN": "variant-secret"}},
    )
    client = _client(tmp_path, monkeypatch, home)

    library = client.get("/api/agent/library")

    assert library.status_code == 200
    body = library.json()
    assert "top-secret" not in library.text
    assert "variant-secret" not in library.text
    assert body["mcp"][0]["id"] == "managed"
    assert body["discovery"]["mcp"][0]["id"] == "native"


def test_library_refreshes_skill_readback_after_external_change(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    source = _skill_source(tmp_path, "# Managed\n")
    client = _client(tmp_path, monkeypatch, home)
    client.post(
        "/api/agent/skills/import-source",
        json={"skill_id": "demo", "name": "Demo", "source": str(source)},
    )
    installed = client.post(
        "/api/agent/resources/reconcile",
        json={
            "action": "install",
            "resource_type": "skill",
            "resource_ids": ["demo"],
            "client_id": "codex",
        },
    )
    assert installed.status_code == 200, installed.text
    (home / ".codex/skills/demo/SKILL.md").write_text(
        "# Externally changed\n", encoding="utf-8"
    )

    resource = client.get("/api/agent/library").json()["skills"][0]

    assert resource["observations"][0]["status"] == "drifted"
    shutil.rmtree(home / ".codex/skills/demo")
    missing = client.get("/api/agent/library").json()["skills"][0]
    assert missing["observations"][0]["status"] == "missing"


def test_library_refreshes_prompt_readback_after_external_change(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text('model = "gpt-5"\n', encoding="utf-8")
    client = _client(tmp_path, monkeypatch, home)
    saved = client.post(
        "/api/agent/prompts",
        json={"prompt_id": "rules", "name": "Rules", "content": "managed\n"},
    )
    assert saved.status_code == 200, saved.text
    installed = client.post(
        "/api/agent/resources/reconcile",
        json={
            "action": "install",
            "resource_type": "prompt",
            "resource_ids": ["rules"],
            "client_id": "codex",
        },
    )
    assert installed.status_code == 200, installed.text
    (home / ".codex/AGENTS.md").write_text("external\n", encoding="utf-8")

    resource = client.get("/api/agent/library").json()["prompts"][0]

    assert resource["observations"][0]["status"] == "drifted"
    (home / ".codex/AGENTS.md").unlink()
    missing = client.get("/api/agent/library").json()["prompts"][0]
    assert missing["observations"][0]["status"] == "missing"


def test_mcp_scan_keeps_database_variant_and_reports_observed_drift(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "changed"\n', encoding="utf-8"
    )
    store = AgentMcpStore(tmp_path / "data/agent")
    store.upsert_server("demo", {"command": "base"})
    store.state.upsert_mcp_variant(
        "demo", "codex", "linux", {"command": "expected"}, source="manual"
    )
    client = _client(tmp_path, monkeypatch, home)

    scanned = client.post(
        "/api/agent/mcp/scan",
        json={"node_ids": ["__local__"], "apps": ["codex"]},
    )

    assert scanned.status_code == 200, scanned.text
    variant = store.state.list_mcp_variants("demo", "codex", "linux")[0]
    assert variant["spec"]["command"] == "expected"
    observation = store.state.list_mcp_observations("__local__", "codex")[0]
    assert observation["status"] == "drifted"
    resource = client.get("/api/agent/library").json()["mcp"][0]
    assert resource["observations"][0]["status"] == "drifted"


def test_library_marks_mcp_readback_drift_against_database_definition(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config = home / ".codex/config.toml"
    config.write_text(
        '[mcp_servers.demo]\ncommand = "actual"\n', encoding="utf-8"
    )
    AgentMcpStore(tmp_path / "data/agent").upsert_server(
        "demo", {"command": "expected"}
    )
    client = _client(tmp_path, monkeypatch, home)

    drifted = client.get("/api/agent/library").json()["mcp"][0]

    assert drifted["observations"][0]["status"] == "drifted"
    config.write_text(
        '[mcp_servers.demo]\ncommand = "expected"\n', encoding="utf-8"
    )
    installed = client.get("/api/agent/library").json()["mcp"][0]
    assert installed["observations"][0]["status"] == "installed"


def test_profile_rejects_secret_values_in_item_config(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch, tmp_path / "home")

    response = client.put(
        "/api/agent/profiles/unsafe",
        json={
            "name": "Unsafe",
            "items": [
                {
                    "client_id": "codex",
                    "resource_type": "router",
                    "resource_id": "default",
                    "config": {"api_key": "must-not-be-stored"},
                }
            ],
        },
    )

    assert response.status_code == 400
    state = PanelStateStore(tmp_path / "data/agent")
    assert state.get_profile("unsafe") is None


def test_delete_mcp_and_prompt_resources_restore_client_projections(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    mcp_path = home / ".codex/config.toml"
    mcp_path.write_text('model = "keep"\n', encoding="utf-8")
    prompt_path = home / ".codex/AGENTS.md"
    prompt_path.write_text("before\n", encoding="utf-8")
    client = _client(tmp_path, monkeypatch, home)
    AgentMcpStore(tmp_path / "data/agent").upsert_server(
        "demo", {"command": "node"}
    )
    from app.services.agent_prompts import AgentPromptStore

    AgentPromptStore(tmp_path / "data/agent").upsert_prompt(
        "rules", "Rules", "after\n"
    )
    for resource_type, resource_id in (("mcp", "demo"), ("prompt", "rules")):
        installed = client.post(
            "/api/agent/resources/reconcile",
            json={
                "action": "install",
                "resource_type": resource_type,
                "resource_ids": [resource_id],
                "client_id": "codex",
            },
        )
        assert installed.status_code == 200, installed.text
        assert installed.json()["verified"] is True

    assert "mcp_servers.demo" in mcp_path.read_text(encoding="utf-8")
    assert prompt_path.read_text(encoding="utf-8") == "after\n"

    deleted_mcp = client.delete("/api/agent/resources/mcp/demo")
    deleted_prompt = client.delete("/api/agent/resources/prompt/rules")

    assert deleted_mcp.status_code == 200, deleted_mcp.text
    assert deleted_prompt.status_code == 200, deleted_prompt.text
    assert "mcp_servers.demo" not in mcp_path.read_text(encoding="utf-8")
    assert prompt_path.read_text(encoding="utf-8") == "before\n"
    state = PanelStateStore(tmp_path / "data/agent")
    assert "demo" not in state.list_mcp_servers()
    assert "rules" not in state.list_prompts()


def test_profile_applies_provider_projection_and_router_selection(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    client = _client(tmp_path, monkeypatch, home)
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore

    AgentProviderStore(tmp_path / "data/agent").upsert_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={
            "type": "codex",
            "config": 'model = "relay-model"\n',
            "routing": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "model": "relay-model",
            },
        },
    )
    saved = client.put(
        "/api/agent/profiles/routed",
        json={
            "name": "Routed",
            "items": [
                {
                    "client_id": "codex",
                    "resource_type": "provider",
                    "resource_id": "relay",
                },
                {
                    "client_id": "codex",
                    "resource_type": "router",
                    "resource_id": "default",
                    "config": {"provider_id": "relay"},
                },
            ],
        },
    )
    assert saved.status_code == 200, saved.text

    applied = client.post(
        "/api/agent/profiles/routed/apply",
        json={"node_id": "__local__", "client_id": "codex"},
    )

    assert applied.status_code == 200, applied.text
    assert applied.json()["verified"] is True
    assert 'model = "relay-model"' in (home / ".codex/config.toml").read_text(
        encoding="utf-8"
    )
    provider = AgentProviderStore(tmp_path / "data/agent").get_provider(
        "codex", "relay"
    )
    assert provider is not None and provider["is_current"] is True
    router = AgentRouterConfigStore(tmp_path / "data/agent").snapshot()
    assert router["provider_ids"]["codex"] == "relay"


def test_prompt_api_round_trips_description_in_library(
    tmp_path: Path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch, tmp_path / "home")

    saved = client.post(
        "/api/agent/prompts",
        json={
            "prompt_id": "rules",
            "name": "Rules",
            "description": "团队编码约定",
            "content": "Use tests first.\n",
        },
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["description"] == "团队编码约定"
    prompt = client.get("/api/agent/library").json()["prompts"][0]
    assert prompt["id"] == "rules"
    assert prompt["description"] == "团队编码约定"


def test_resource_order_api_persists_each_category_and_provider_scope(
    tmp_path: Path, monkeypatch
) -> None:
    state = PanelStateStore(tmp_path / "data/agent")
    for resource_id in ("first", "second"):
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
        for client_id in ("codex", "claude"):
            state.upsert_agent_provider(
                app_id=client_id,
                provider_id=resource_id,
                name=resource_id.title(),
                settings={"type": client_id},
            )
    client = _client(tmp_path, monkeypatch, tmp_path / "home")

    for resource_type in ("mcp", "skill", "prompt", "profile"):
        response = client.put(
            "/api/agent/resources/order",
            json={
                "resource_type": resource_type,
                "resource_ids": ["second", "first"],
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["resource_ids"] == ["second", "first"]

    provider = client.put(
        "/api/agent/resources/order",
        json={
            "resource_type": "provider",
            "resource_ids": ["second", "first"],
            "client_id": "codex",
        },
    )
    assert provider.status_code == 200, provider.text

    restarted = PanelStateStore(tmp_path / "data/agent")
    assert list(restarted.list_mcp_servers()) == ["second", "first"]
    assert list(restarted.list_skills()) == ["second", "first"]
    assert list(restarted.list_prompts()) == ["second", "first"]
    assert [item["id"] for item in restarted.list_profiles()] == ["second", "first"]
    assert [
        item["id"] for item in restarted.list_agent_providers("codex")["codex"]
    ] == ["second", "first"]
    assert [
        item["id"] for item in restarted.list_agent_providers("claude")["claude"]
    ] == ["first", "second"]


def test_resource_order_api_rejects_duplicate_incomplete_and_unscoped_provider(
    tmp_path: Path, monkeypatch
) -> None:
    state = PanelStateStore(tmp_path / "data/agent")
    state.upsert_mcp_server("one", {"command": "one"})
    state.upsert_mcp_server("two", {"command": "two"})
    state.upsert_agent_provider(
        app_id="codex",
        provider_id="one",
        name="One",
        settings={"type": "codex"},
    )
    client = _client(tmp_path, monkeypatch, tmp_path / "home")

    duplicate = client.put(
        "/api/agent/resources/order",
        json={"resource_type": "mcp", "resource_ids": ["one", "one"]},
    )
    incomplete = client.put(
        "/api/agent/resources/order",
        json={"resource_type": "mcp", "resource_ids": ["one"]},
    )
    unscoped = client.put(
        "/api/agent/resources/order",
        json={"resource_type": "provider", "resource_ids": ["one"]},
    )

    assert duplicate.status_code == 400
    assert incomplete.status_code == 409
    assert unscoped.status_code == 400


def test_mcp_api_can_explicitly_clear_description(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, tmp_path / "home")
    first = client.post(
        "/api/agent/mcp/servers",
        json={
            "server_id": "demo",
            "name": "Demo",
            "description": "temporary note",
            "spec": {"command": "demo"},
        },
    )
    assert first.status_code == 200, first.text

    cleared = client.post(
        "/api/agent/mcp/servers",
        json={
            "server_id": "demo",
            "name": "Demo",
            "description": "",
            "spec": {"command": "demo"},
        },
    )

    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["description"] == ""
