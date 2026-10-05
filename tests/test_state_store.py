import json
from pathlib import Path

import pytest

from app.models.assets import AssetSnapshot
from app.registry.service import RegistryService
from app.services.agent_mcp import AgentMcpStore
from app.services.agent_prompts import AgentPromptStore
from app.services.assets import AssetService
from app.services.state_store import PanelStateStore, resolve_state_db_path


def _write_minimal_registry(root: Path) -> None:
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "categories" / "node.yaml").write_text(
        "id: node\nlabel: Node\norder: 30\nenabled: true\n", encoding="utf-8"
    )
    (root / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (root / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )


def test_state_db_path_resolves_shared_data_db(tmp_path: Path) -> None:
    assert resolve_state_db_path(tmp_path / "config") == tmp_path / "data" / "state.db"
    assert (
        resolve_state_db_path(tmp_path / "data" / "agent")
        == tmp_path / "data" / "state.db"
    )
    assert (
        resolve_state_db_path(tmp_path / "custom.sqlite3")
        == tmp_path / "custom.sqlite3"
    )


def test_panel_state_store_persists_asset_snapshots(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")
    asset = AssetSnapshot(
        object_id="node__codex",
        category="node",
        name="@openai/codex",
        status="installed",
        current_version="1.0.0",
        metadata={"manager": "npm"},
        actionable=True,
    )

    store.replace_asset_snapshots("node", [asset])

    assert (tmp_path / "data" / "state.db").exists()
    assert store.list_asset_snapshots("node")[0].object_id == "node__codex"
    assert store.get_asset_snapshot("node__codex").metadata["manager"] == "npm"  # type: ignore[union-attr]


def test_agent_mcp_store_migrates_legacy_json_into_state_db(tmp_path: Path) -> None:
    data_root = tmp_path / "data" / "agent"
    data_root.mkdir(parents=True)
    (data_root / "mcp_servers.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {
                        "id": "demo",
                        "name": "demo",
                        "spec": {"command": "node"},
                        "apps": {"codex": True},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    store = AgentMcpStore(data_root)

    assert (tmp_path / "data" / "state.db").exists()
    assert store.list_servers()["demo"]["spec"]["command"] == "node"
    store.upsert_server("demo", {"command": "uvx"}, {"claude": True})
    reloaded = AgentMcpStore(data_root).list_servers()["demo"]
    assert reloaded["spec"]["command"] == "uvx"
    assert reloaded["apps"]["codex"] is True
    assert reloaded["apps"]["claude"] is True


def test_agent_prompt_store_persists_in_state_db(tmp_path: Path) -> None:
    data_root = tmp_path / "data" / "agent"
    store = AgentPromptStore(data_root)

    store.upsert_prompt("default", "默认提示词", "hello")

    assert (tmp_path / "data" / "state.db").exists()
    assert AgentPromptStore(data_root).get_prompt("default")["content"] == "hello"  # type: ignore[index]
    assert store.delete_prompt("default") is True
    assert AgentPromptStore(data_root).list_prompts() == {}


def test_asset_service_writes_category_snapshot_to_state_db(tmp_path: Path) -> None:
    _write_minimal_registry(tmp_path)
    asset = AssetSnapshot(
        object_id="node__demo",
        category="node",
        name="demo",
        status="installed",
        current_version="0.1.0",
    )
    service = AssetService(
        RegistryService(tmp_path), node_scanner=lambda: [asset], config_root=tmp_path
    )

    assert service.list_assets("node")[0].object_id == "node__demo"

    cached = PanelStateStore(tmp_path).list_asset_snapshots("node")
    assert cached[0].name == "demo"


def test_agent_provider_store_persists_in_state_db(tmp_path: Path) -> None:
    from app.services.agent_providers import AgentProviderStore

    store = AgentProviderStore(tmp_path / "data" / "agent")
    saved = store.upsert_provider(
        app_id="codex",
        provider_id="team",
        name="Team Codex",
        settings={"type": "codex", "config": 'model = "gpt-5"'},
        is_current=True,
    )

    assert saved["is_current"] is True
    reloaded = AgentProviderStore(tmp_path / "data" / "agent").get_provider(
        "codex", "team"
    )
    assert reloaded is not None
    assert reloaded["settings_config"]["config"] == 'model = "gpt-5"'
    from app.services.agent_providers import CurrentProviderError

    with pytest.raises(CurrentProviderError, match="current provider"):
        store.delete_provider("codex", "team")
    assert store.get_provider("codex", "team") is not None


def test_panel_state_store_persists_mcp_variant_assignment_and_observation(
    tmp_path: Path,
) -> None:
    store = PanelStateStore(tmp_path / "config")
    store.upsert_mcp_server("context7", {"command": "npx"}, {"codex": True})

    store.upsert_mcp_variant(
        "context7",
        "codex",
        "linux",
        {"command": "npx", "args": ["context7"]},
        source="scan",
    )
    store.replace_mcp_assignments(
        "__local__",
        "codex",
        [
            {
                "mcp_id": "context7",
                "variant_client_id": "codex",
                "variant_platform": "linux",
            }
        ],
    )
    store.replace_mcp_observations(
        "__local__",
        "codex",
        [
            {
                "mcp_id": "context7",
                "present": True,
                "spec_hash": "abc123",
                "public_spec": {"command": "npx"},
                "status": "installed",
            }
        ],
    )

    assert store.list_mcp_variants("context7")[0]["client_id"] == "codex"
    assert store.list_mcp_assignments("__local__", "codex")[0]["mcp_id"] == "context7"
    observation = store.list_mcp_observations("__local__", "codex")[0]
    assert observation["status"] == "installed"
    assert observation["public_spec"] == {"command": "npx"}


def test_legacy_mcp_targets_do_not_create_installed_observations(
    tmp_path: Path,
) -> None:
    store = PanelStateStore(tmp_path / "config")
    store.upsert_mcp_server("legacy", {"command": "node"}, {"codex": True})

    assert store.list_mcp_observations("__local__", "codex") == []


def test_remove_mcp_assignments_removes_only_selected_ids(tmp_path: Path) -> None:
    store = PanelStateStore(tmp_path / "config")
    store.upsert_mcp_server("remove", {"command": "x"}, {})
    store.upsert_mcp_server("keep", {"command": "y"}, {})
    store.replace_mcp_assignments(
        "__local__",
        "codex",
        [
            {"mcp_id": "remove", "variant_client_id": "codex", "variant_platform": "linux"},
            {"mcp_id": "keep", "variant_client_id": "codex", "variant_platform": "linux"},
        ],
    )

    removed = store.remove_mcp_assignments("__local__", "codex", {"remove"})

    assert removed == 1
    assert [item["mcp_id"] for item in store.list_mcp_assignments("__local__", "codex")] == ["keep"]
