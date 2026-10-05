"""Provider intent only becomes current after the actual writer and readback succeed."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.services.agent_mcp import agent_data_root
from app.services.agent_providers import AgentProviderStore
from app.tasks.store import InMemoryTaskStore
from tests.app_factory import create_app
from tests.test_agent_workbench import _write_agent_category


def fixture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    path = home / ".codex/config.toml"
    path.write_text('model = "old"\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    config = tmp_path / "config"
    _write_agent_category(config)
    store = AgentProviderStore(agent_data_root(config))
    for name in ["old", "new"]:
        store.upsert_provider(
            app_id="codex",
            provider_id=name,
            name=name,
            settings={"auth": {}, "config": f'model = "{name}"\n'},
            is_current=name == "old",
        )
    tasks = InMemoryTaskStore()
    app = create_app(config_root=config, task_store=tasks, node_scanner=lambda: [])
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return app, client, store, tasks, path


@pytest.mark.parametrize("writer", ["exit 7", "true"])
def test_failed_or_noop_writer_keeps_old_current(tmp_path, monkeypatch, writer):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.api.agent.build_provider_apply_shell", lambda *a, **kw: writer
    )
    response = client.post("/api/agent/providers/codex/new/activate", json={})
    assert response.status_code == 202
    assert store.get_provider("codex", "old")["is_current"]
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert store.get_provider("codex", "old")["is_current"]
    assert path.read_text() == 'model = "old"\n'


def test_successful_writer_commits_only_after_worker_readback(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    assert (
        client.post("/api/agent/providers/codex/new/activate", json={}).status_code
        == 202
    )
    assert store.get_provider("codex", "old")["is_current"]
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "succeeded"
    assert store.get_provider("codex", "new")["is_current"]
    assert '"new"' in path.read_text()


def test_partial_writer_failure_restores_native_files(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    writer = f"printf 'model = \"partial\"\\n' > {str(path)!r}; exit 7"
    monkeypatch.setattr(
        "app.api.agent.build_provider_apply_shell", lambda *a, **kw: writer
    )
    client.post("/api/agent/providers/codex/new/activate", json={})
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert path.read_text() == 'model = "old"\n'
    assert store.get_provider("codex", "old")["is_current"]


def test_takeover_started_after_enqueue_prevents_native_writer(tmp_path, monkeypatch):
    from app.services.agent_router_config import AgentRouterConfigStore

    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    client.post("/api/agent/providers/codex/new/activate", json={})
    AgentRouterConfigStore(store.data_root).set_takeover("codex", True)
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert path.read_text() == 'model = "old"\n'
    assert store.get_provider("codex", "old")["is_current"]


def test_provider_readback_separates_external_drift_from_current(tmp_path, monkeypatch):
    from app.services.agent_workbench import _provider_public_rows

    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    path.write_text('model = "external"\n')
    rows = _provider_public_rows(store, store.list_providers("codex"), path.parents[1])
    old = next(r for r in rows["codex"] if r["id"] == "old")
    assert old["live_state"] == "drifted"
    assert old["is_current"] is False


def test_synchronous_profile_also_requires_readback(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.api.agent.build_provider_apply_shell", lambda *a, **kw: "true"
    )
    result = client.post(
        "/api/agent/resources/reconcile",
        json={
            "node_id": "__local__",
            "client_id": "codex",
            "resource_type": "provider",
            "resource_ids": ["new"],
            "action": "install",
        },
    )
    assert result.status_code == 200
    assert result.json()["verified"] is False
    assert store.get_provider("codex", "old")["is_current"]


def test_task_page_hides_provider_execution_payload(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.api.agent.build_provider_apply_shell", lambda *a, **kw: "true"
    )
    secret = "fixture-task-key-never-display"
    store.upsert_provider(
        app_id="codex",
        provider_id="new",
        name="new",
        settings={"auth": {"OPENAI_API_KEY": secret}, "config": 'model = "new"\n'},
    )
    client.post("/api/agent/providers/codex/new/activate", json={})
    task = tasks.list_all()[0]
    from app.api.tasks import _build_task_item

    public = _build_task_item(task)
    assert secret not in str(public)
    assert "base64.b64decode" not in public["plan"]
    assert secret not in client.get("/tasks").text


@pytest.mark.parametrize(
    "client_id",
    ["claude", "codex", "gemini", "grokbuild", "opencode", "openclaw", "hermes"],
)
@pytest.mark.parametrize("write_secrets", [True, False])
def test_native_projection_matches_each_client_writer(
    tmp_path, monkeypatch, client_id, write_secrets
):
    import subprocess
    from app.services.agent_provider_adapters import build_native_settings
    from app.services.agent_provider_projection import provider_matches
    from app.services.agent_providers import build_provider_apply_shell

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    settings = build_native_settings(
        client_id,
        {
            "provider_key": "relay",
            "profile": "relay",
            "base_url": "https://fixture.example/v1",
            "model": "test-model",
            "api_key": "fixture-secret",
            "api_format": "openai_chat",
            "auth_mode": "bearer",
            "headers": {"Authorization": "Bearer fixture-secret"},
        },
    )
    provider = {
        "id": "relay",
        "app_id": client_id,
        "name": "relay",
        "settings_config": settings,
    }
    subprocess.run(
        [
            "bash",
            "-c",
            build_provider_apply_shell(provider, write_secrets=write_secrets),
        ],
        check=True,
    )
    assert provider_matches(home, provider, write_secrets=write_secrets)


@pytest.mark.parametrize("client_id", ["opencode", "openclaw", "hermes"])
@pytest.mark.parametrize("fails", [False, True])
def test_additive_remove_commits_after_readback_and_restores_failure(
    tmp_path, monkeypatch, client_id, fails
):
    import subprocess
    from app.services.agent_provider_adapters import (
        build_native_settings,
        read_provider_records,
    )
    from app.services.agent_providers import build_provider_apply_shell

    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    home = path.parents[1]
    for name in ["remove-me", "keep-me"]:
        settings = build_native_settings(
            client_id, {"model": "fixture", "base_url": "https://fixture.example/v1"}
        )
        provider = store.upsert_provider(
            app_id=client_id,
            provider_id=name,
            name=name,
            settings=settings,
            is_current=name == "remove-me",
        )
        subprocess.run(["bash", "-c", build_provider_apply_shell(provider)], check=True)
    if fails:
        monkeypatch.setattr(
            "app.api.agent.build_provider_remove_shell", lambda *a, **kw: "true"
        )
    result = client.post(
        f"/api/agent/providers/{client_id}/remove-me/remove-live", json={}
    )
    assert result.status_code == 202
    assert store.get_provider(client_id, "remove-me")["is_current"]
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == ("failed" if fails else "succeeded")
    ids = {r["provider_id"] for r in read_provider_records(home, client_id)}
    assert ids == ({"remove-me", "keep-me"} if fails else {"keep-me"})
    assert store.get_provider(client_id, "remove-me")["is_current"] is fails


def test_permission_failure_rolls_back_before_current_commit(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    client.post("/api/agent/providers/codex/new/activate", json={})
    original = Path.chmod

    def fail_native_chmod(self, *args, **kwargs):
        if self == path:
            raise OSError("fixture chmod failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", fail_native_chmod)
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert store.get_provider("codex", "old")["is_current"]
    assert path.read_text() == 'model = "old"\n'


def test_noop_writer_detects_native_fields_outside_provider_form(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    store.upsert_provider(
        app_id="codex",
        provider_id="new",
        name="new",
        settings={
            "auth": {},
            "config": 'model = "old"\nmodel_reasoning_effort = "high"\n',
        },
    )
    monkeypatch.setattr(
        "app.api.agent.build_provider_apply_shell", lambda *a, **kw: "true"
    )
    client.post("/api/agent/providers/codex/new/activate", json={})
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert store.get_provider("codex", "old")["is_current"]


def test_edited_resource_invalidates_queued_projection(tmp_path, monkeypatch):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    client.post("/api/agent/providers/codex/new/activate", json={})
    store.upsert_provider(
        app_id="codex",
        provider_id="new",
        name="new",
        settings={"auth": {}, "config": 'model = "edited-after-queue"\n'},
    )
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert store.get_provider("codex", "old")["is_current"]
    assert path.read_text() == 'model = "old"\n'
