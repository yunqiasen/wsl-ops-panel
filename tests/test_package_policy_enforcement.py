"""F07 regression: Node/Python cross-entry-point package policy enforcement.

Every enqueue path (local API, remote API bulk, remote HTML form) must consult
AssetPolicyService before queueing install_or_update / delete actions.

Rules:
  - delete on a package not allowed by policy → skip with reason
  - install_or_update on a brand-new package (no rule) → always allow
  - install_or_update on a known package → check actionable + allowed_actions
  - custom_command parsed names follow the same rules
  - policy source is the current panel config, applied uniformly to local + remote
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.tasks.store import InMemoryTaskStore
from tests.app_factory import create_app


# ── helpers ──────────────────────────────────────────────────────────────────


def _rules(
    root: Path,
    *,
    node_yaml: str = "packages: []\n",
    python_yaml: str = "packages: []\n",
):
    d = root / "rules"
    d.mkdir(parents=True, exist_ok=True)
    (d / "node-packages.yaml").write_text(node_yaml, encoding="utf-8")
    (d / "python-packages.yaml").write_text(python_yaml, encoding="utf-8")


def _setup_node_category(root: Path):
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "categories" / "node.yaml").write_text(
        "id: node\nlabel: Node\norder: 30\nenabled: true\n", encoding="utf-8"
    )
    (root / "objects").mkdir(parents=True, exist_ok=True)


def _setup_remote_category(root: Path):
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "categories" / "remote.yaml").write_text(
        "id: remote\nlabel: SSH\norder: 70\nenabled: true\n", encoding="utf-8"
    )
    (root / "objects").mkdir(parents=True, exist_ok=True)


_AGENT_NODE_RULES = """\
packages:
  - name: "@openai/codex"
    managed_by: agent
    allowed_actions: [update_latest, deploy_version]
    blocked_reason: 保留给 Agent 专项维护
  - name: "@anthropic-ai/claude-code"
    managed_by: agent
    protected: true
    blocked_reason: 受保护包，禁止直接操作
"""

_WHITELIST_PYTHON_RULES = """\
packages:
  - name: fastapi
    managed_by: python
    allowed_actions: [update_latest, deploy_version, delete, full_delete]
  - name: openai
    managed_by: python
    allowed_actions: [update_latest, deploy_version]
"""


# ── 1. Python delete blocked (local API, whitelist) ──────────────────────────


def test_python_delete_non_whitelisted_local_blocked(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "package_names": ["outside-policy"],
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["queued_count"] == 0
    assert len(payload["skipped"]) == 1
    assert "outside-policy" in payload["skipped"][0]
    assert "白名单" in payload["skipped"][0]


# ── 2. Python delete blocked (remote bulk API) ───────────────────────────────


def test_python_delete_non_whitelisted_remote_bulk_blocked(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    # create an online remote node
    client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    resp = client.post(
        "/api/remote/actions/package-bulk",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "package_names": ["outside-policy"],
            "node_ids": ["vps"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 0
    assert len(resp.json()["skipped"]) == 1
    assert "outside-policy" in resp.json()["skipped"][0]


# ── 3. Node delete blocked — agent-managed, delete not in allowed_actions ────


def test_node_delete_agent_managed_local_blocked(tmp_path):
    _setup_node_category(tmp_path)
    _rules(tmp_path, node_yaml=_AGENT_NODE_RULES)
    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_action": "delete",
            "package_names": ["@openai/codex"],
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["queued_count"] == 0
    assert len(payload["skipped"]) == 1
    assert "@openai/codex" in payload["skipped"][0]


# ── 4. Node delete blocked — protected package ───────────────────────────────


def test_node_delete_protected_local_blocked(tmp_path):
    _setup_node_category(tmp_path)
    _rules(tmp_path, node_yaml=_AGENT_NODE_RULES)
    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_action": "delete",
            "package_names": ["@anthropic-ai/claude-code"],
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 0
    assert len(resp.json()["skipped"]) == 1
    assert "@anthropic-ai/claude-code" in resp.json()["skipped"][0]


# ── 5. Node delete blocked (remote bulk) ─────────────────────────────────────


def test_node_delete_agent_managed_remote_bulk_blocked(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, node_yaml=_AGENT_NODE_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    resp = client.post(
        "/api/remote/actions/package-bulk",
        json={
            "tool_type": "node",
            "package_action": "delete",
            "package_names": ["@openai/codex"],
            "node_ids": ["vps"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 0


# ── 6. New package install allowed (Python, not in whitelist) ────────────────


def test_python_install_new_package_local_allowed(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_action": "install_or_update",
            "package_names": ["brand-new-pkg"],
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 1


# ── 7. Agent-managed node update allowed (install_or_update) ─────────────────


def test_node_install_agent_managed_local_allowed(tmp_path):
    _setup_node_category(tmp_path)
    _rules(tmp_path, node_yaml=_AGENT_NODE_RULES)
    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_action": "install_or_update",
            "package_names": ["@openai/codex"],
            "version_map": {"@openai/codex": "1.2.3"},
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 1


# ── 8. Python delete whitelisted package allowed ─────────────────────────────


def test_python_delete_whitelisted_local_allowed(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "package_names": ["fastapi"],
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 1


# ── 9. Python update blocked — allowed_actions lacks update ───────────────────


def test_python_update_action_not_allowed_blocked(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    # openai is whitelisted but only allows [update_latest, deploy_version]
    # — install_or_update maps to {update_latest, deploy_version} so this should pass
    # Instead test a package with only delete allowed
    python_yaml = """\
packages:
  - name: locked-pkg
    managed_by: python
    allowed_actions: [delete, full_delete]
"""
    _rules(tmp_path, python_yaml=python_yaml)
    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_action": "install_or_update",
            "package_names": ["locked-pkg"],
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 0
    assert "locked-pkg" in resp.json()["skipped"][0]


# ── 10. custom_command with delete respects policy ───────────────────────────


def test_custom_command_delete_respects_policy(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "install_command": "python3 -m pip uninstall -y outside-policy",
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 0
    assert len(resp.json()["skipped"]) >= 1


# ── 11. custom_command install blocked for protected package ──────────────────


def test_custom_command_install_protected_blocked(tmp_path):
    _setup_node_category(tmp_path)
    _rules(tmp_path, node_yaml=_AGENT_NODE_RULES)
    store = InMemoryTaskStore()
    client = TestClient(
        create_app(config_root=tmp_path, task_store=store, node_scanner=lambda: [])
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_action": "install_or_update",
            "install_command": "npm install -g @anthropic-ai/claude-code",
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 0
    assert "@anthropic-ai/claude-code" in resp.json()["skipped"][0]


# ── 12. custom_command install new package allowed ───────────────────────────


def test_custom_command_install_new_package_allowed(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "node",
            "package_action": "install_or_update",
            "install_command": "npm install -g brand-new-tool",
            "node_ids": ["__local__"],
        },
    )
    assert resp.status_code == 202
    assert resp.json()["queued_count"] == 1


# ── 13. Remote HTML form delete blocked ───────────────────────────────────────


def test_remote_html_form_delete_blocked(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, node_yaml=_AGENT_NODE_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    resp = client.post(
        "/remote/actions/package",
        data={
            "tool_type": "node",
            "package_action": "delete",
            "package_name": "@openai/codex",
            "version": "",
            "node_ids": "vps",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(store.list_all()) == 0


# ── 14. Remote HTML form install allowed for new package ──────────────────────


def test_remote_html_form_install_new_allowed(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    resp = client.post(
        "/remote/actions/package",
        data={
            "tool_type": "node",
            "package_action": "install_or_update",
            "package_name": "brand-new-tool",
            "version": "",
            "node_ids": "vps",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(store.list_all()) == 1


# ── 15. Mixed: some packages blocked, some allowed in bulk ───────────────────


def test_bulk_mixed_blocked_and_allowed(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    resp = client.post(
        "/api/remote/actions/package-bulk",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "package_names": ["fastapi", "outside-policy"],
            "node_ids": ["vps"],
        },
    )
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["queued_count"] == 1  # only fastapi
    assert len(payload["skipped"]) == 1
    assert "outside-policy" in payload["skipped"][0]


# ── 16. Policy applies identically to local and remote (same package) ────────


def test_policy_identical_local_and_remote(tmp_path):
    _setup_remote_category(tmp_path)
    _rules(tmp_path, python_yaml=_WHITELIST_PYTHON_RULES)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/remote/nodes",
        json={
            "name": "VPS",
            "host": "example.test",
            "username": "root",
            "auth_type": "key",
            "key_path": "/tmp/id",
        },
    )
    client.app.state.remote_node_store.update_node_status("vps", status="online")

    # delete outside-policy on local
    local_resp = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "package_names": ["outside-policy"],
            "node_ids": ["__local__"],
        },
    )
    # delete outside-policy on remote
    remote_resp = client.post(
        "/api/remote/actions/package-bulk",
        json={
            "tool_type": "python",
            "package_action": "delete",
            "package_names": ["outside-policy"],
            "node_ids": ["vps"],
        },
    )
    assert local_resp.json()["queued_count"] == 0
    assert remote_resp.json()["queued_count"] == 0
    # Both skip reasons mention the package
    assert "outside-policy" in local_resp.json()["skipped"][0]
    assert "outside-policy" in remote_resp.json()["skipped"][0]
