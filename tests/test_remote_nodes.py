import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.models.remote_nodes import RemoteNodeCreate
from app.services.remote_nodes import (
    RemoteNodeStore,
    RemoteSSHService,
    default_remote_nodes_path,
)


def _write_remote_category(root: Path) -> None:
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "categories" / "remote.yaml").write_text(
        "id: remote\nlabel: SSH 与配置同步\norder: 70\nenabled: true\n",
        encoding="utf-8",
    )
    (root / "objects").mkdir(parents=True, exist_ok=True)


def test_remote_node_store_adds_and_deletes_nodes(tmp_path: Path) -> None:
    store = RemoteNodeStore(tmp_path / "remote_nodes.yaml")
    node = store.add_node(
        RemoteNodeCreate(
            name="MacBook", host="100.64.0.1", username="div", password="pw"
        )
    )

    assert node.id == "macbook"
    assert store.list_nodes()[0].host == "100.64.0.1"
    assert store.delete_node("macbook") is True
    assert store.list_nodes() == []


def test_remote_ssh_service_builds_key_login_command() -> None:
    captured = {}

    def runner(command, timeout):
        captured["command"] = command
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(
            command, 0, stdout="connected:Linux", stderr=""
        )

    store = RemoteNodeStore("/tmp/nonexistent-remote-nodes.yaml")
    node = store.add_node(
        RemoteNodeCreate(
            name="VPS",
            host="example.test",
            username="root",
            auth_type="key",
            key_path="/tmp/id",
        )
    )
    ok, message = RemoteSSHService(runner=runner).test_connection(node)

    assert ok is True
    assert message == "connected:Linux"
    assert captured["command"][:2] == ["ssh", "-o"]
    assert "-i" in captured["command"]
    assert "root@example.test" in captured["command"]


def test_remote_category_page_and_node_api(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    category = client.get("/categories/remote")
    assert category.status_code == 200
    assert "节点中心" in category.text
    assert "配置同步中心" in category.text

    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "WSL",
            "host": "100.126.43.55",
            "username": "div",
            "password": "pw",
        },
    )
    assert created.status_code == 201
    assert created.json()["id"] == "wsl"

    listed = client.get("/api/remote/nodes")
    assert listed.status_code == 200
    assert listed.json()["nodes"][0]["host"] == "100.126.43.55"

    page = client.get("/remote/nodes")
    assert page.status_code == 200
    assert "100.126.43.55" in page.text


def test_default_remote_nodes_path_uses_config_sibling_data_dir(tmp_path: Path) -> None:
    assert default_remote_nodes_path(Path("config")) == Path("data/remote_nodes.yaml")
    assert (
        default_remote_nodes_path(tmp_path) == tmp_path / "data" / "remote_nodes.yaml"
    )


def test_remote_package_scan_allows_empty_package_name(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    assert created.status_code == 201

    response = client.post(
        "/remote/actions/package",
        data={
            "tool_type": "node",
            "package_action": "scan",
            "package_name": "",
            "version": "",
            "node_ids": "vps",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_remote_category_keeps_package_tools_out_of_config_sync(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/remote")

    assert response.status_code == 200
    assert "系统配置同步中心" in response.text
    assert "SSH 客户端配置" in response.text
    assert "/etc/environment" in response.text
    assert "当前 WSL" in response.text
    assert "Node 远程维护" not in response.text
    assert "Python 远程维护" not in response.text
    assert "Agent 远程扫描" not in response.text


def test_node_category_uses_package_catalog_instead_of_top_bulk_actions(
    tmp_path: Path,
) -> None:
    (tmp_path / "categories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "categories" / "node.yaml").write_text(
        "id: node\nlabel: Node\norder: 30\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (tmp_path / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    assert created.status_code == 201

    from app.scanners.node_scanner import parse_npm_package

    client.app.state.node_scanner = lambda: [parse_npm_package("update@0.7.4")]
    client.app.state.rebuild_registry_runtime()
    response = client.get("/categories/node")

    assert response.status_code == 200
    assert "执行设备" in response.text
    assert "当前 WSL" in response.text
    assert "VPS" in response.text
    assert "package-install-panel" in response.text
    assert 'class="page-hero"' not in response.text
    assert "data-bulk-toolbar" not in response.text
    assert response.text.index("data-remote-target-panel") > response.text.index(
        "package-install-panel"
    )
    assert "data-package-version-select" in response.text
    assert "data-package-delete-selected" in response.text
    assert "data-package-action-strip" in response.text
    assert "data-package-card-version-select" in response.text
    assert 'href="/assets/node__' in response.text
    assert 'data-package-detail-url="/assets/node__' in response.text
    assert "package-card-title" in response.text
    assert response.text.index("data-package-action-strip") > response.text.index(
        "data-remote-target-panel"
    )
    assert response.text.index("data-package-action-strip") < response.text.index(
        "custom-install-row"
    )
    assert 'data-bulk-action="update-latest"' not in response.text
    assert "顶部批量更新只管资产卡" not in response.text


def test_remote_package_bulk_api_queues_each_package_for_each_node(
    tmp_path: Path,
) -> None:
    _write_remote_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    assert created.status_code == 201
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    response = client.post(
        "/api/remote/actions/package-bulk",
        json={
            "tool_type": "node",
            "package_action": "install_or_update",
            "package_names": ["@openai/codex", "@google/gemini-cli"],
            "version_map": {"@openai/codex": "0.2.9"},
            "node_ids": ["vps"],
        },
    )

    assert response.status_code == 202
    assert response.json()["queued_count"] == 2
    tasks = store.list_all()
    assert len(tasks) == 2
    assert tasks[0].requested_version == "0.2.9"


def test_agent_category_renders_agent_workbench(tmp_path: Path) -> None:
    (tmp_path / "categories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "categories" / "agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (tmp_path / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/agent")

    assert response.status_code == 200
    assert "Codex" in response.text
    assert "Claude Code" in response.text
    assert "Agent 工作台" in response.text
    assert "MCP 管理" in response.text
    assert "Prompts 管理" in response.text
    assert "Skills 管理" in response.text
    assert "扫描选中 Agent 配置" not in response.text


def test_node_category_shows_install_catalog_and_offline_ssh_device(
    tmp_path: Path,
) -> None:
    (tmp_path / "categories").mkdir(parents=True, exist_ok=True)
    (tmp_path / "categories" / "node.yaml").write_text(
        "id: node\nlabel: Node\norder: 30\nenabled: true\n", encoding="utf-8"
    )
    (tmp_path / "objects").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rules" / "node-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    (tmp_path / "rules" / "python-packages.yaml").write_text(
        "packages: []\n", encoding="utf-8"
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    assert created.status_code == 201

    response = client.get("/categories/node")

    assert response.status_code == 200
    assert "package-install-panel" in response.text
    assert "@openai/codex" in response.text
    assert "data-package-manager-select" in response.text
    assert "data-package-version-select" in response.text
    assert "data-package-delete-selected" in response.text
    assert "package-suggestion-preview" in response.text
    assert "device-chip--offline" in response.text
    assert 'value="vps" disabled' in response.text


def test_package_install_api_queues_local_install_task(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore
    import json

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_names": ["@openai/codex"],
            "version_map": {"@openai/codex": "1.2.3"},
            "node_ids": ["__local__"],
        },
    )

    assert response.status_code == 202
    assert response.json()["queued_count"] == 1
    task = store.list_all()[0]
    plan = json.loads(Path(task.plan_path).read_text(encoding="utf-8"))
    assert plan["commands"] == [["npm", "install", "-g", "@openai/codex@1.2.3"]]


def test_package_status_api_reports_local_status(tmp_path: Path, monkeypatch) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="installed\n", stderr="")

    monkeypatch.setattr("app.api.remote_nodes.subprocess.run", fake_run)

    response = client.post(
        "/api/packages/status",
        json={
            "tool_type": "python",
            "package_names": ["fastapi"],
            "node_ids": ["__local__"],
        },
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["status"] == "installed"


def test_package_catalog_suggestions_exclude_installed_and_include_history(
    tmp_path: Path,
) -> None:
    from app.models.assets import AssetSnapshot
    from app.services.package_catalog import (
        build_package_catalog,
        record_package_history,
    )

    installed = [
        AssetSnapshot(
            object_id="node__pnpm",
            category="node",
            name="pnpm",
            status="installed",
            current_version="9.0.0",
        )
    ]
    catalog = build_package_catalog("node", installed, config_root=tmp_path)

    suggestion_names = {item["name"] for item in catalog["suggestions"]}
    assert "pnpm" not in suggestion_names
    assert "typescript" in suggestion_names
    assert (
        next(item for item in catalog["suggestions"] if item["name"] == "typescript")[
            "install_command"
        ]
        == "npm install -g typescript"
    )

    record_package_history(tmp_path, "node", ["left-pad"], "npm install -g left-pad")
    catalog = build_package_catalog("node", installed, config_root=tmp_path)
    assert any(
        item["name"] == "left-pad" and item["source"] == "历史"
        for item in catalog["items"]
    )


def test_package_install_api_parses_install_command_and_records_history(
    tmp_path: Path,
) -> None:
    _write_remote_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore
    import json

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "install_command": "npm install -g pnpm@9.0.0",
            "node_ids": ["__local__"],
        },
    )

    assert response.status_code == 202
    assert response.json()["queued_count"] == 1
    task = store.list_all()[0]
    plan = json.loads(Path(task.plan_path).read_text(encoding="utf-8"))
    assert plan["commands"] == [["bash", "-lc", "npm install -g pnpm@9.0.0"]]
    history = (tmp_path / "data" / "package_catalog_history.json").read_text(
        encoding="utf-8"
    )
    assert "pnpm" in history


def test_package_install_api_accepts_card_version_map_for_multiple_tools(
    tmp_path: Path,
) -> None:
    _write_remote_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore
    import json

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_names": ["@openai/codex", "tsx"],
            "version_map": {"@openai/codex": "0.141.0", "tsx": "4.20.0"},
            "node_ids": ["__local__"],
        },
    )

    assert response.status_code == 202
    plans = [
        json.loads(Path(task.plan_path).read_text(encoding="utf-8"))
        for task in store.list_all()
    ]
    assert ["npm", "install", "-g", "@openai/codex@0.141.0"] in [
        plan["commands"][0] for plan in plans
    ]
    assert ["npm", "install", "-g", "tsx@4.20.0"] in [
        plan["commands"][0] for plan in plans
    ]


def test_package_versions_api_reports_latest_and_history(
    tmp_path: Path, monkeypatch
) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    from app.models.assets import PackageVersionInfo

    def fake_node_versions(self, package_name):
        assert package_name == "@openai/codex"
        return PackageVersionInfo(
            latest_version="1.2.3", versions=["1.0.0", "1.2.3"], source_status="ok"
        )

    monkeypatch.setattr(
        "app.api.remote_nodes.PackageVersionService.get_node_version_info",
        fake_node_versions,
    )

    response = client.get(
        "/api/packages/versions?tool_type=node&package_name=%40openai%2Fcodex"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["latest_version"] == "1.2.3"
    assert payload["versions"][0] == "1.2.3"
    assert "1.0.0" in payload["versions"]


def test_remote_category_renders_config_file_modules(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get("/categories/remote")

    assert response.status_code == 200
    assert "config-file-card" in response.text
    assert "config-sync-workflow" in response.text
    assert "data-config-sync-panel" in response.text
    assert 'data-config-preset="daily"' in response.text
    assert 'name="sync_kind"' in response.text
    assert "~/.zshrc" in response.text
    assert "/etc/wsl.conf" in response.text
    assert "SSH 客户端配置" in response.text
    assert "Shell 启动文件" not in response.text


def test_config_scan_queues_each_selected_module(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/remote/actions/config-scan",
        data={"sync_kind": ["zshrc", "gitconfig"], "node_ids": ["__local__"]},
        follow_redirects=False,
    )

    assert response.status_code == 303
    actions = [task.action for task in store.list_all()]
    assert "remote_config_scan_zshrc" in actions
    assert "remote_config_scan_gitconfig" in actions


def test_config_modules_expose_os_compatibility_and_windows_profile(
    tmp_path: Path,
) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/remote/nodes",
        json={
            "name": "Win",
            "host": "example.test",
            "username": "Administrator",
            "password": "pw",
            "os_hint": "windows",
        },
    )

    response = client.get("/categories/remote")

    assert response.status_code == 200
    assert 'data-config-compatible="linux,wsl,macos"' in response.text
    assert "PowerShell Profile" in response.text
    assert 'data-config-compatible="windows"' in response.text
    assert "Windows 不适用的配置会自动跳过" in response.text
    assert "data-config-scan-form" in response.text
    assert "data-config-apply-form" in response.text
    assert "data-config-apply-nodes" in response.text
    assert (
        'data-config-node-os="windows"' in response.text
        or 'data-node-os="windows"' in response.text
    )


def test_config_apply_append_line_skips_incompatible_windows_node(
    tmp_path: Path,
) -> None:
    _write_remote_category(tmp_path)
    from app.tasks.store import InMemoryTaskStore

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "Win",
            "host": "example.test",
            "username": "Administrator",
            "password": "pw",
            "os_hint": "windows",
        },
    )
    assert created.status_code == 201
    client.app.state.remote_node_store.update_node_status("win", status="online")

    response = client.post(
        "/remote/actions/config-apply",
        data={
            "module_id": "zshrc",
            "operation": "append_line",
            "content": "export DEMO=1",
            "node_ids": ["__local__", "win"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    tasks = store.list_all()
    assert len(tasks) == 1
    assert tasks[0].action == "config_apply_zshrc_append_line"


def test_config_module_page_saves_draft_without_touching_source(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    page = client.get("/remote/config-modules/zshrc")
    assert page.status_code == 200
    assert "编辑草稿" in page.text
    assert "后续同步策略" in page.text
    assert "~/.zshrc" in page.text

    response = client.post(
        "/remote/config-modules/zshrc",
        data={"draft_text": "# draft only"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert (tmp_path / "data" / "config_sync_drafts" / "zshrc.txt").read_text(
        encoding="utf-8"
    ) == "# draft only"


def test_remote_api_and_task_command_do_not_expose_password(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    created = client.post(
        "/api/remote/nodes",
        json={
            "name": "Secure",
            "host": "example.test",
            "username": "div",
            "password": "top-secret",
        },
    )

    assert created.status_code == 201
    assert "password" not in created.json()
    listed = client.get("/api/remote/nodes")
    assert "top-secret" not in listed.text
    assert listed.json()["nodes"][0]["has_password"] is True

    node = client.app.state.remote_node_store.get_node("secure")
    command = client.app.state.remote_ssh_service.build_command(node, "echo ok")
    assert "top-secret" not in command
    assert command[:2] == ["sshpass", "-f"]
    credential_file = Path(command[2])
    assert credential_file.read_text(encoding="utf-8") == "top-secret"
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "data" / "remote_nodes.yaml").stat().st_mode & 0o777 == 0o600


def test_remote_node_rejects_ssh_option_injection(tmp_path: Path) -> None:
    _write_remote_category(tmp_path)
    client = TestClient(create_app(config_root=tmp_path))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(
        "/api/remote/nodes",
        json={
            "name": "Bad",
            "host": "-oProxyCommand=bad",
            "username": "div",
            "password": "pw",
        },
    )

    assert response.status_code == 422
