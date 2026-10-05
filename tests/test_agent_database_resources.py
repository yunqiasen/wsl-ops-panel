from __future__ import annotations

from pathlib import Path

from app.services.state_store import PanelStateStore


RESOURCE_TABLES = {
    "agent_skills",
    "agent_skill_variants",
    "agent_skill_assignments",
    "agent_skill_observations",
    "agent_prompt_variants",
    "agent_prompt_assignments",
    "agent_prompt_observations",
    "agent_router_nodes",
    "agent_router_clients",
    "agent_route_failover_queue",
    "agent_profiles",
    "agent_profile_items",
    "agent_settings",
}


def test_agent_resource_schema_is_created_idempotently(tmp_path: Path) -> None:
    first = PanelStateStore(tmp_path / "config")
    first.close()

    second = PanelStateStore(tmp_path / "config")

    assert RESOURCE_TABLES <= second.list_table_names()


def test_agent_setting_round_trips_json_values(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")

    store.set_agent_setting("migration", {"done": True, "count": 2})

    assert store.get_agent_setting("migration") == {"done": True, "count": 2}
    assert store.get_agent_setting("missing", "fallback") == "fallback"


def test_router_snapshot_round_trips_normalized_tables(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")
    payload = {
        "schema_version": 1,
        "listen_address": "127.0.0.1",
        "listen_port": 7888,
        "show_home_switch": True,
        "outbound_proxy": "socks5://127.0.0.1:1080",
        "providers": {
            "codex": {
                "base_url": "https://relay.example/v1",
                "api_format": "openai_responses",
                "api_key": "secret",
                "max_retries": 2,
            }
        },
        "provider_ids": {"codex": "relay"},
        "takeover": {"codex": True},
        "failover_queues": {"codex": ["relay", "backup"]},
    }

    store.replace_router_snapshot("__local__", payload)
    loaded = store.get_router_snapshot("__local__")

    assert loaded["listen_port"] == 7888
    assert loaded["providers"]["codex"]["base_url"] == "https://relay.example/v1"
    assert loaded["provider_ids"] == {"codex": "relay"}
    assert loaded["takeover"] == {"codex": True}
    assert loaded["failover_queues"] == {"codex": ["relay", "backup"]}


def test_profile_items_cascade_when_profile_is_deleted(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")
    store.upsert_profile(
        "work",
        "Work",
        "demo profile",
        [
            {
                "client_id": "codex",
                "resource_type": "mcp",
                "resource_id": "context7",
                "config": {"enabled": True},
                "sort_index": 0,
            }
        ],
    )

    assert store.list_profiles()[0]["items"][0]["resource_id"] == "context7"
    assert store.delete_profile("work") is True
    assert store.list_profiles() == []


def _create_skill(path: Path, content: str = "# Demo\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(content, encoding="utf-8")


def test_skill_import_creates_ssot_without_touching_client(tmp_path: Path) -> None:
    from app.services.agent_skill_store import AgentSkillStore

    source = tmp_path / "source"
    _create_skill(source)
    home = tmp_path / "home"
    store = AgentSkillStore(tmp_path / "data/agent")

    saved = store.import_source("demo", "Demo", str(source))

    assert Path(saved["ssot_path"], "SKILL.md").read_text(encoding="utf-8") == "# Demo\n"
    assert not (home / ".codex/skills/demo").exists()
    assert AgentSkillStore(tmp_path / "data/agent").get("demo")["content_hash"]


def test_skill_install_and_uninstall_change_target_state_but_keep_library(
    tmp_path: Path,
) -> None:
    from app.services.agent_skill_store import AgentSkillStore

    source = tmp_path / "source"
    _create_skill(source)
    home = tmp_path / "home"
    store = AgentSkillStore(tmp_path / "data/agent")
    store.import_source("demo", "Demo", str(source))

    installed = store.install_to_client(home, "demo", "codex", mode="copy")

    assert installed["verified"] is True
    assert (home / ".codex/skills/demo/SKILL.md").is_file()
    assignments = store.state.list_skill_assignments("__local__", "codex")
    assert assignments[0]["skill_id"] == "demo"
    observations = store.state.list_skill_observations("__local__", "codex")
    assert observations[0]["status"] == "installed"

    removed = store.uninstall_from_client(home, "demo", "codex")

    assert removed["verified"] is True
    assert store.get("demo") is not None
    assert Path(store.get("demo")["ssot_path"]).is_dir()  # type: ignore[index]
    assert store.state.list_skill_assignments("__local__", "codex") == []


def test_skill_reimport_updates_ssot_hash_and_keeps_previous_backup(tmp_path: Path) -> None:
    from app.services.agent_skill_store import AgentSkillStore

    source = tmp_path / "source"
    _create_skill(source, "# V1\n")
    store = AgentSkillStore(tmp_path / "data/agent")
    first = store.import_source("demo", "Demo", str(source))
    (source / "SKILL.md").write_text("# V2\n", encoding="utf-8")

    second = store.import_source("demo", "Demo", str(source))

    assert second["content_hash"] != first["content_hash"]
    assert Path(second["ssot_path"], "SKILL.md").read_text(encoding="utf-8") == "# V2\n"
    assert any(store.backup_root.iterdir())


def test_skill_delete_requires_assignments_to_be_removed(tmp_path: Path) -> None:
    import pytest

    from app.services.agent_skill_store import AgentSkillStore, SkillAssignedError

    source = tmp_path / "source"
    _create_skill(source)
    store = AgentSkillStore(tmp_path / "data/agent")
    store.import_source("demo", "Demo", str(source))
    store.state.set_skill_assignment("__local__", "codex", "demo")

    with pytest.raises(SkillAssignedError, match="分配"):
        store.delete("demo")

    store.state.remove_skill_assignments("__local__", "codex", {"demo"})
    assert store.delete("demo") is True
    assert store.get("demo") is None


def test_skill_import_from_client_copies_into_library(tmp_path: Path) -> None:
    from app.services.agent_skill_store import AgentSkillStore

    home = tmp_path / "home"
    source = home / ".codex/skills/native"
    _create_skill(source, "# Native\n")
    store = AgentSkillStore(tmp_path / "data/agent")

    saved = store.import_from_client(home, "codex", "native")

    assert Path(saved["ssot_path"], "SKILL.md").read_text(encoding="utf-8") == "# Native\n"
    assert store.state.list_skill_assignments("__local__", "codex")[0]["skill_id"] == "native"


def test_prompt_resolves_client_variant_then_base_content(tmp_path: Path) -> None:
    from app.services.agent_prompts import AgentPromptStore

    store = AgentPromptStore(tmp_path / "data/agent")
    store.upsert_prompt("rules", "Rules", "base\n")
    store.upsert_variant("rules", "claude", "linux", "claude\n")

    assert store.resolve_content("rules", "claude", "linux") == "claude\n"
    assert store.resolve_content("rules", "codex", "linux") == "base\n"


def test_prompt_install_and_restore_track_assignment_and_observation(
    tmp_path: Path,
) -> None:
    from app.services.agent_prompts import AgentPromptStore

    data_root = tmp_path / "data/agent"
    home = tmp_path / "home"
    target = home / ".codex/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    store = AgentPromptStore(data_root)
    store.upsert_prompt("rules", "Rules", "after\n")

    installed = store.install_local(home, "codex", "rules")

    assert installed["verified"] is True
    assert target.read_text(encoding="utf-8") == "after\n"
    assert store.state.list_prompt_assignments("__local__", "codex")[0]["prompt_id"] == "rules"
    assert store.state.list_prompt_observations("__local__", "codex")[0]["status"] == "installed"

    restored = store.restore_local(home, "codex")

    assert restored["verified"] is True
    assert target.read_text(encoding="utf-8") == "before\n"
    assert store.state.list_prompt_assignments("__local__", "codex") == []


def test_prompt_import_current_creates_library_variant_without_rewriting_file(
    tmp_path: Path,
) -> None:
    from app.services.agent_prompts import AgentPromptStore

    home = tmp_path / "home"
    target = home / ".codex/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("native\n", encoding="utf-8")
    store = AgentPromptStore(tmp_path / "data/agent")

    saved = store.import_current(home, "codex", prompt_id="codex-current", name="当前")

    assert saved["content"] == "native\n"
    assert store.resolve_content("codex-current", "codex", "linux") == "native\n"
    assert target.read_text(encoding="utf-8") == "native\n"
    assert store.state.list_prompt_assignments("__local__", "codex")[0]["prompt_id"] == "codex-current"


def test_mcp_import_keeps_one_logical_resource_with_client_variants(
    tmp_path: Path,
) -> None:
    from app.services.agent_mcp import AgentMcpStore

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.demo]\ncommand = "node"\n', encoding="utf-8"
    )
    (home / ".claude.json").write_text(
        '{"mcpServers":{"demo":{"command":"uvx","args":["demo"]}}}',
        encoding="utf-8",
    )
    store = AgentMcpStore(tmp_path / "data/agent")

    imported = store.import_from_home(home, apps=["codex", "claude"])

    assert imported == 2
    assert set(store.list_servers()) == {"demo"}
    variants = store.state.list_mcp_variants("demo")
    assert {(item["client_id"], item["spec"]["command"]) for item in variants} == {
        ("codex", "node"),
        ("claude", "uvx"),
    }
    assert {
        (item["client_id"], item["mcp_id"])
        for item in store.state.list_mcp_assignments("__local__")
    } == {("codex", "demo"), ("claude", "demo")}


def test_workbench_keeps_observed_mcp_outside_managed_library(tmp_path: Path) -> None:
    from app.services.agent_workbench import build_agent_workbench_context

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.native-only]\ncommand = "node"\n', encoding="utf-8"
    )

    context = build_agent_workbench_context(
        tmp_path / "config", home=home, which=lambda _name: None
    )

    assert context["agent_mcp_servers"] == []
    assert context["agent_mcp_discovery"][0]["id"] == "native-only"
    assert context["agent_mcp_discovery"][0]["managed"] is False


def test_router_json_migrates_once_then_sqlite_wins(tmp_path: Path) -> None:
    import json

    from app.services.agent_router_config import AgentRouterConfigStore

    data_root = tmp_path / "data/agent"
    data_root.mkdir(parents=True)
    legacy = data_root / "router.json"
    legacy.write_text(
        json.dumps({"listen_port": 7999, "takeover": {"codex": True}}),
        encoding="utf-8",
    )

    first = AgentRouterConfigStore(data_root)
    assert first.snapshot()["listen_port"] == 7999
    assert first.snapshot()["takeover"] == {"codex": True}
    assert legacy.stat().st_mode & 0o777 == 0o600

    first.update_global(listen_port=7888)
    legacy.write_text(json.dumps({"listen_port": 7000}), encoding="utf-8")
    restarted = AgentRouterConfigStore(data_root)

    assert restarted.snapshot()["listen_port"] == 7888
    assert restarted.state.get_agent_setting("router_json_migrated_v1")["migrated"] is True
    assert json.loads(legacy.read_text(encoding="utf-8"))["listen_port"] == 7000


def _seed_reconcile_skill(state: PanelStateStore, tmp_path: Path, skill_id: str) -> None:
    state.upsert_skill(
        skill_id=skill_id,
        name=skill_id.title(),
        source_kind="local",
        ssot_path=str(tmp_path / "skills" / skill_id),
        content_hash=f"hash-{skill_id}",
    )


def test_profile_store_crud_and_client_snapshot(tmp_path: Path) -> None:
    from app.services.agent_profiles import AgentProfileStore

    store = AgentProfileStore(tmp_path / "data/agent")
    saved = store.upsert(
        "work",
        "Work",
        description="daily setup",
        items=[
            {
                "client_id": "codex",
                "resource_type": "mcp",
                "resource_id": "context7",
            },
            {
                "client_id": "claude",
                "resource_type": "prompt",
                "resource_id": "rules",
                "config": {"platform": "linux"},
            },
        ],
    )

    assert saved["id"] == "work"
    assert store.get("work")["description"] == "daily setup"
    snapshot = store.snapshot_client("work", "codex")
    assert snapshot["client_id"] == "codex"
    assert [item["resource_id"] for item in snapshot["items"]] == ["context7"]
    assert AgentProfileStore(tmp_path / "data/agent").list()[0]["id"] == "work"
    assert store.delete("work") is True
    assert store.get("work") is None


def test_profile_plan_only_changes_different_resources(tmp_path: Path) -> None:
    from app.services.agent_mcp import AgentMcpStore
    from app.services.agent_profiles import AgentProfileStore
    from app.services.agent_reconciler import AgentReconciler

    data_root = tmp_path / "data/agent"
    AgentMcpStore(data_root).upsert_server("context7", {"command": "npx"})
    _seed_reconcile_skill(PanelStateStore(data_root), tmp_path, "review")
    AgentProfileStore(data_root).upsert(
        "work",
        "Work",
        items=[
            {"client_id": "codex", "resource_type": "mcp", "resource_id": "context7"},
            {"client_id": "codex", "resource_type": "skill", "resource_id": "review"},
        ],
    )

    plan = AgentReconciler(data_root).plan_profile(
        "work",
        node_id="__local__",
        observations={"mcp": {"context7": "installed"}},
    )

    assert [(item.action, item.resource_id) for item in plan.operations] == [
        ("install", "review")
    ]
    assert plan.warnings == []


def test_profile_plan_marks_drift_and_isolates_selected_client(tmp_path: Path) -> None:
    from app.services.agent_mcp import AgentMcpStore
    from app.services.agent_profiles import AgentProfileStore
    from app.services.agent_reconciler import AgentReconciler

    data_root = tmp_path / "data/agent"
    AgentMcpStore(data_root).upsert_server("demo", {"command": "node"})
    AgentProfileStore(data_root).upsert(
        "team",
        "Team",
        items=[
            {"client_id": "codex", "resource_type": "mcp", "resource_id": "demo"},
            {"client_id": "claude", "resource_type": "mcp", "resource_id": "demo"},
        ],
    )

    plan = AgentReconciler(data_root).plan_profile(
        "team",
        client_id="codex",
        observations={"mcp": {"demo": "drifted"}},
    )

    assert [(item.client_id, item.action, item.resource_id) for item in plan.operations] == [
        ("codex", "update", "demo")
    ]


def test_profile_plan_reports_dangling_resource_without_operation(tmp_path: Path) -> None:
    from app.services.agent_profiles import AgentProfileStore
    from app.services.agent_reconciler import AgentReconciler

    data_root = tmp_path / "data/agent"
    AgentProfileStore(data_root).upsert(
        "broken",
        "Broken",
        items=[
            {"client_id": "codex", "resource_type": "skill", "resource_id": "missing"}
        ],
    )

    plan = AgentReconciler(data_root).plan_profile("broken")

    assert plan.operations == []
    assert any("missing" in warning for warning in plan.warnings)


def test_agent_resource_descriptions_and_order_round_trip(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")
    store.upsert_mcp_server("zeta", {"command": "zeta"}, name="Zeta", description="MCP Z")
    store.upsert_mcp_server("alpha", {"command": "alpha"}, name="Alpha", description="MCP A")
    store.upsert_skill(
        skill_id="zeta",
        name="Zeta",
        source_kind="local",
        ssot_path=str(tmp_path / "skills/zeta"),
        content_hash="zeta-hash",
        description="Skill Z",
    )
    store.upsert_skill(
        skill_id="alpha",
        name="Alpha",
        source_kind="local",
        ssot_path=str(tmp_path / "skills/alpha"),
        content_hash="alpha-hash",
        description="Skill A",
    )
    store.upsert_prompt("zeta", "Zeta", "zeta\n", description="Prompt Z")
    store.upsert_prompt("alpha", "Alpha", "alpha\n", description="Prompt A")
    store.upsert_profile("zeta", "Zeta", "Profile Z", [])
    store.upsert_profile("alpha", "Alpha", "Profile A", [])
    store.upsert_agent_provider(
        app_id="codex", provider_id="zeta", name="Zeta", settings={"type": "codex"}, notes="Provider Z"
    )
    store.upsert_agent_provider(
        app_id="codex", provider_id="alpha", name="Alpha", settings={"type": "codex"}, notes="Provider A"
    )

    assert list(store.list_mcp_servers()) == ["zeta", "alpha"]
    assert list(store.list_skills()) == ["zeta", "alpha"]
    assert list(store.list_prompts()) == ["zeta", "alpha"]
    assert [item["id"] for item in store.list_profiles()] == ["zeta", "alpha"]
    assert [item["id"] for item in store.list_agent_providers("codex")["codex"]] == ["zeta", "alpha"]
    assert store.list_prompts()["zeta"]["description"] == "Prompt Z"

    for resource_type in ("mcp", "skill", "prompt", "profile"):
        assert store.reorder_agent_resources(resource_type, ["alpha", "zeta"]) == ["alpha", "zeta"]
    assert store.reorder_agent_resources("provider", ["alpha", "zeta"], client_id="codex") == ["alpha", "zeta"]
    store.close()

    restarted = PanelStateStore(tmp_path / "config")
    assert list(restarted.list_mcp_servers()) == ["alpha", "zeta"]
    assert list(restarted.list_skills()) == ["alpha", "zeta"]
    assert list(restarted.list_prompts()) == ["alpha", "zeta"]
    assert [item["id"] for item in restarted.list_profiles()] == ["alpha", "zeta"]
    assert [item["id"] for item in restarted.list_agent_providers("codex")["codex"]] == ["alpha", "zeta"]
    assert restarted.list_prompts()["zeta"]["description"] == "Prompt Z"


def test_provider_schema_adds_meta_json_to_existing_database(tmp_path: Path) -> None:
    import sqlite3

    db_path = tmp_path / "data" / "state.db"
    db_path.parent.mkdir(parents=True)
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE agent_providers (
            id TEXT NOT NULL,
            app_id TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            website_url TEXT,
            category TEXT,
            notes TEXT,
            icon TEXT,
            icon_color TEXT,
            is_current INTEGER NOT NULL DEFAULT 0,
            sort_index INTEGER NOT NULL DEFAULT 0,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (id, app_id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO agent_providers (
            id, app_id, name, settings_json, created_at, updated_at
        ) VALUES ('manual', 'codex', 'Manual', '{}', 'before', 'before')
        """
    )
    connection.commit()
    connection.close()

    store = PanelStateStore(db_path)

    columns = {
        row["name"]
        for row in store._connection.execute(
            "PRAGMA table_info(agent_providers)"
        ).fetchall()
    }
    assert "meta_json" in columns
    assert store.get_agent_provider("codex", "manual")["meta"] == {}


def test_provider_meta_round_trips_database_methods(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")

    saved = store.upsert_agent_provider(
        app_id="codex",
        provider_id="relay",
        name="Relay",
        settings={"type": "codex", "config": 'model = "gpt-5"\n'},
        meta={"api_format": "openai_responses", "region": "us-east"},
    )

    columns = {
        row["name"]
        for row in store._connection.execute(
            "PRAGMA table_info(agent_providers)"
        ).fetchall()
    }
    assert "meta_json" in columns
    assert saved["meta"] == {
        "api_format": "openai_responses",
        "region": "us-east",
    }
    assert store.get_agent_provider("codex", "relay")["meta"] == saved["meta"]
    assert store.list_agent_providers("codex")["codex"][0]["meta"] == saved["meta"]


def test_local_current_provider_migration_preserves_record_and_updates_references(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    store = PanelStateStore(config_root)
    store.upsert_agent_provider(
        app_id="codex",
        provider_id="first",
        name="First",
        settings={"type": "codex"},
    )
    legacy = store.upsert_agent_provider(
        app_id="codex",
        provider_id="local-current",
        name="当前本机配置",
        settings={
            "type": "codex",
            "routing": {
                "base_url": "https://relay.example/v1",
                "secret_ref": "provider-secret:codex:local-current",
            },
        },
        notes="保留描述",
        is_current=True,
        source="import",
    )
    store.replace_router_snapshot(
        "__local__",
        {
            "schema_version": 1,
            "provider_ids": {"codex": "local-current"},
            "failover_queues": {"codex": ["first", "local-current"]},
        },
    )
    store.upsert_profile(
        "work",
        "Work",
        "provider profile",
        [
            {
                "client_id": "codex",
                "resource_type": "provider",
                "resource_id": "local-current",
            }
        ],
    )
    store.close()

    restarted = PanelStateStore(config_root)

    migrated = restarted.get_agent_provider("codex", "default")
    assert migrated is not None
    assert restarted.get_agent_provider("codex", "local-current") is None
    assert migrated["sort_index"] == legacy["sort_index"]
    assert migrated["is_current"] is True
    assert migrated["notes"] == "保留描述"
    assert migrated["settings_config"]["routing"] == {
        "base_url": "https://relay.example/v1",
        "secret_ref": "provider-secret:codex:default",
    }
    router = restarted.get_router_snapshot("__local__")
    assert router["provider_ids"]["codex"] == "default"
    assert router["failover_queues"]["codex"] == ["first", "default"]
    assert restarted.list_profiles()[0]["items"][0]["resource_id"] == "default"


def test_local_current_provider_migration_keeps_both_when_default_exists(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    store = PanelStateStore(config_root)
    expected_default = store.upsert_agent_provider(
        app_id="codex",
        provider_id="default",
        name="User Default",
        settings={"type": "codex", "config": "user-default"},
        notes="do not overwrite",
    )
    expected_legacy = store.upsert_agent_provider(
        app_id="codex",
        provider_id="local-current",
        name="Legacy",
        settings={"type": "codex", "config": "legacy"},
        is_current=True,
    )
    store.replace_router_snapshot(
        "__local__",
        {"schema_version": 1, "provider_ids": {"codex": "local-current"}},
    )
    store.close()

    restarted = PanelStateStore(config_root)

    assert restarted.get_agent_provider("codex", "default") == expected_default
    assert restarted.get_agent_provider("codex", "local-current") == expected_legacy
    assert restarted.get_router_snapshot("__local__")["provider_ids"]["codex"] == (
        "local-current"
    )


def test_agent_resource_reorder_rejects_incomplete_or_duplicate_ids(tmp_path: Path) -> None:
    import pytest

    store = PanelStateStore(tmp_path / "config")
    store.upsert_mcp_server("one", {"command": "one"})
    store.upsert_mcp_server("two", {"command": "two"})
    store.upsert_agent_provider(
        app_id="codex", provider_id="one", name="One", settings={"type": "codex"}
    )

    with pytest.raises(ValueError, match="完整"):
        store.reorder_agent_resources("mcp", ["one"])
    with pytest.raises(ValueError, match="重复"):
        store.reorder_agent_resources("mcp", ["one", "one"])
    with pytest.raises(ValueError, match="client_id"):
        store.reorder_agent_resources("provider", ["one"])


def test_agent_resource_schema_migrates_description_and_sort_columns(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")

    columns = {
        table: {
            row["name"]
            for row in store._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for table in (
            "agent_mcp_servers",
            "agent_skills",
            "agent_prompts",
            "agent_profiles",
            "agent_providers",
        )
    }

    assert "description" in columns["agent_prompts"]
    assert all("sort_index" in values for values in columns.values())
