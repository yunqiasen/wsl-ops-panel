"""Restart the real app over the same private state, replacing only OS/network."""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.core.security import COOKIE_NAME, issue_session_token
from app.services.cf_tunnels import CFTunnelService
from app.tasks.executor import execute_action_plan
from tests.app_factory import create_app
from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


def restore_app(panel):
    old = panel.app.state.cf_service
    fresh = CFTunnelService(old.data_root)
    fresh.runtime = old.runtime
    fresh.http_transport = old.http_transport
    fresh.clock = old.clock
    app = create_app(
        config_root=panel.app.state.config_root,
        task_store=panel.store,
        cf_service=fresh,
        docker_scanner=lambda: panel.containers,
        **{
            name: lambda: []
            for name in (
                "project_scanner",
                "systemd_scanner",
                "node_scanner",
                "python_scanner",
                "host_process_scanner",
                "system_infra_scanner",
            )
        },
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return app, client


def test_startup_revalidates_existing_verified_tunnel_without_creating_another(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
    finally:
        panel.app.state.task_worker.stop()
    observed.requests.clear()
    _, client = restore_app(panel)
    with client:
        status = client.get("/api/assets/web/cf").json()
        assert status["state"] == "online"
        assert len(runtime.resources) == 1
        assert any("trycloudflare.com" in request for request in observed.requests)


def test_startup_cleans_unverified_resource_left_by_interrupted_executor(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)

    class CrashedRuntime(FakeTunnelRuntime):
        crash = True

        def observe(self, handle, **kwargs):
            if self.crash:
                self.crash = False
                raise SystemExit("fixture executor terminated")
            return super().observe(handle, **kwargs)

    runtime = CrashedRuntime()
    panel.app.state.cf_service.runtime = runtime
    response = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    ).json()
    task = panel.store.get(response["tasks"][0]["id"])
    plan = panel.app.state.task_worker.load_plan(Path(task.plan_path))
    with pytest.raises(SystemExit):
        execute_action_plan(
            task,
            plan,
            sink=panel.app.state.system_terminal_sink,
            store=panel.store,
            cf_executor=panel.app.state.cf_service.execute,
        )
    assert panel.store.get(task.id).status == "running"
    assert next(iter(runtime.resources.values()))["active"] is True
    _, client = restore_app(panel)
    with client:
        status = client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "persistence"
        assert status["current_url"] is None
        assert panel.store.get(task.id).status == "interrupted"
        assert not any(resource["active"] for resource in runtime.resources.values())


def test_restart_compensates_when_cf_finished_but_task_result_write_failed(
    tmp_path, monkeypatch
):
    import sqlite3

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    original_update = panel.store.update

    def failing_result(task):
        if task.status == "succeeded":
            raise sqlite3.OperationalError("fixture result write failed")
        original_update(task)

    monkeypatch.setattr(panel.store, "update", failing_result)
    response = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    ).json()
    task = panel.store.get(response["tasks"][0]["id"])
    plan = panel.app.state.task_worker.load_plan(Path(task.plan_path))
    with pytest.raises(sqlite3.OperationalError):
        execute_action_plan(
            task,
            plan,
            sink=panel.app.state.system_terminal_sink,
            store=panel.store,
            cf_executor=panel.app.state.cf_service.execute,
        )
    assert panel.store.get(task.id).status == "running"
    assert next(iter(runtime.resources.values()))["verified"] is True
    _, client = restore_app(panel)
    with client:
        status = client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "persistence"
        assert status["current_url"] is None
        assert panel.store.get(task.id).status == "interrupted"
        assert not any(resource["active"] for resource in runtime.resources.values())


def test_restart_reclaims_a_launch_interrupted_before_its_handle_was_returned(
    tmp_path, monkeypatch
):
    import subprocess
    from tests.test_cf_deadlines import DelayedManager

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    manager = DelayedManager(tmp_path, monkeypatch)
    manager.delay = manager.next_delay = 0
    manager_run = subprocess.run

    def crash_after_creation(command, **kwargs):
        result = manager_run(command, **kwargs)
        if command[0] == "systemd-run":
            raise SystemExit("fixture executor died during registration")
        return result

    monkeypatch.setattr(subprocess, "run", crash_after_creation)
    response = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    ).json()
    task = panel.store.get(response["tasks"][0]["id"])
    plan = panel.app.state.task_worker.load_plan(Path(task.plan_path))
    with pytest.raises(SystemExit):
        execute_action_plan(
            task,
            plan,
            sink=panel.app.state.system_terminal_sink,
            store=panel.store,
            cf_executor=panel.app.state.cf_service.execute,
        )
    assert manager.info["ActiveState"] == "active"
    _, client = restore_app(panel)
    with client:
        status = client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "persistence"
        assert status["current_url"] is None
        assert manager.info["ActiveState"] == "inactive"
        assert panel.store.get(task.id).status == "interrupted"
