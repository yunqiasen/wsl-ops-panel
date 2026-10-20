"""CF behavior through the same authenticated HTTP/queue interface as the UI."""

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import yaml

from app.core.security import COOKIE_NAME, issue_session_token
from app.models.assets import DockerContainerSnapshot
from app.tasks.store import InMemoryTaskStore
from tests.app_factory import create_app


def make_panel(tmp_path: Path, **overrides):
    config = tmp_path / "config"
    project = tmp_path / "business"
    project.mkdir(exist_ok=True)
    (project / "compose.yaml").write_text(
        "services:\n  web:\n    image: example/web:1\n", encoding="utf-8"
    )
    (config / "categories").mkdir(parents=True, exist_ok=True)
    (config / "objects").mkdir(exist_ok=True)
    (config / "categories/docker.yaml").write_text(
        "id: docker\nlabel: Docker\norder: 10\n", encoding="utf-8"
    )
    (config / "objects/web.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "web",
                "name": "Web",
                "category": "docker",
                "type": "docker_compose",
                "config": {
                    "project_dir": str(project),
                    "compose_file": "compose.yaml",
                    "primary_container": "business-web",
                    "compose_service": "web",
                },
            }
        ),
        encoding="utf-8",
    )
    containers = [
        DockerContainerSnapshot(
            id="a" * 64,
            name="business-web",
            image="example/web:1",
            state="running",
            status="Up",
            ports="127.0.0.1:18080->8080/tcp",
            compose_project="business",
            compose_service="web",
            compose_working_dir=str(project),
            labels={
                "com.docker.compose.project": "business",
                "com.docker.compose.service": "web",
                "com.docker.compose.project.working_dir": str(project),
            },
        )
    ]
    store = InMemoryTaskStore()
    app = create_app(
        config_root=config,
        docker_scanner=lambda: containers,
        project_scanner=lambda: [],
        systemd_scanner=lambda: [],
        node_scanner=lambda: [],
        python_scanner=lambda: [],
        host_process_scanner=lambda: [],
        system_infra_scanner=lambda: [],
        task_store=store,
        **overrides,
    )
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return SimpleNamespace(
        app=app, client=client, store=store, project=project, containers=containers
    )


def test_viewing_new_project_cf_is_off_without_creating_a_tunnel(
    tmp_path: Path,
) -> None:
    panel = make_panel(tmp_path)
    response = panel.client.get("/api/assets/web/cf")

    assert response.status_code == 200
    assert response.json()["state"] == "off"
    assert response.json()["desired"] == "off"
    assert response.json()["current_url"] is None
    assert panel.store.list_all() == []
    assert not (tmp_path / "data/cf").exists()
    assert [p.name for p in panel.project.iterdir()] == ["compose.yaml"]


def attach_local_observation(panel, monkeypatch):
    """Replace Docker and HTTP only, leaving the entire CF/application path real."""
    import json
    import subprocess
    import httpx

    info = {
        "id": "a" * 64,
        "name": "/business-web",
        "running": True,
        "started_at": "2026-10-20T00:00:00Z",
        "network_mode": "business_default",
        "ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]},
        "networks": {"business_default": {"IPAddress": "172.20.0.2"}},
        "labels": dict(panel.containers[0].labels),
    }
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(
            200,
            text="<html><title>Business fixture</title></html>",
            headers={"content-type": "text/html"},
        )

    panel.app.state.cf_service.http_client = httpx.Client(
        transport=httpx.MockTransport(respond), trust_env=False
    )
    return SimpleNamespace(info=info, requests=requests)


def test_prepare_cf_uses_verified_published_port_without_starting_a_tunnel(
    tmp_path: Path, monkeypatch
) -> None:
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)

    response = panel.client.get("/api/assets/web/cf/targets")

    assert response.status_code == 200
    targets = response.json()["targets"]
    assert len(targets) == 1
    assert targets[0]["origin"] == "http://127.0.0.1:18080"
    assert targets[0]["source"] == "published_port"
    assert targets[0]["reachable"] is True
    assert targets[0]["id"] and targets[0]["revision"]
    assert observed.requests == ["http://127.0.0.1:18080/"]
    assert panel.client.get("/api/assets/web/cf").json()["state"] == "off"
    assert panel.store.list_all() == []
    assert not (tmp_path / "data/cf").exists()


class FakeTunnelRuntime:
    """A user-manager substitute; the CF lifecycle and persistence stay real."""

    def __init__(self):
        self.resources = {}
        self.boot_id = "boot-fixture"

    def start(self, owner, token, origin, *, timeout):
        handle = {
            "owner": owner,
            "token": token,
            "invocation_id": token,
            "unit": f"fixture-{owner}.service",
            "boot_id": self.boot_id,
        }
        self.resources[token] = {
            "handle": handle,
            "origin": origin,
            "active": True,
            "verified": False,
            "url": "https://business-fixture.trycloudflare.com",
        }
        return handle

    def observe(self, handle, *, timeout=5):
        resource = self.resources.get(handle["token"])
        return {
            "active": bool(resource and resource["active"]),
            "url": resource["url"] if resource else None,
        }

    def activate(self, handle):
        self.resources[handle["token"]]["verified"] = True

    def stop(self, handle, *, timeout=15):
        if handle["token"] in self.resources:
            self.resources[handle["token"]]["active"] = False


def wait_for_task(panel, task_id):
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = panel.store.get(task_id)
        if task and task.status in {"succeeded", "failed", "interrupted"}:
            return task
        time.sleep(0.01)
    raise AssertionError(f"task did not settle: {task_id}")


def test_open_cf_is_verified_and_does_not_block_following_tasks(tmp_path, monkeypatch):
    import sys
    from app.adapters.base import ActionPlan

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    response = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    )
    assert response.status_code == 202
    assert response.json()["queued_count"] == 1, response.json()
    queued = response.json()["tasks"][0]
    assert panel.client.get("/api/assets/web/cf").json()["state"] == "starting"
    assert panel.client.get("/api/assets/web/cf").json()["current_url"] is None
    ordinary = panel.app.state.task_queue.enqueue(
        "ordinary",
        "start",
        plan=ActionPlan(commands=[[sys.executable, "-c", "print('next task')"]]),
    )
    panel.app.state.task_worker.start()
    try:
        assert wait_for_task(panel, queued["id"]).status == "succeeded"
        assert wait_for_task(panel, ordinary.id).status == "succeeded"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "online"
        assert status["current_url"] == "https://business-fixture.trycloudflare.com/"
        assert status["last_verified_at"]
        assert "https://business-fixture.trycloudflare.com/" in observed.requests
        assert len(runtime.resources) == 1
        assert next(iter(runtime.resources.values()))["active"] is True
        assert next(iter(runtime.resources.values()))["verified"] is True
        assert panel.containers[0].state == "running"
        assert [p.name for p in panel.project.iterdir()] == ["compose.yaml"]
    finally:
        panel.app.state.task_worker.stop()


def submit_cf(panel, action):
    response = panel.client.post(
        f"/api/bulk/actions/{action}", json={"asset_ids": ["web"]}
    )
    assert response.status_code == 202
    assert response.json()["queued_count"] == 1, response.json()
    return wait_for_task(panel, response.json()["tasks"][0]["id"])


def test_close_cf_is_idempotent_even_after_origin_stops(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        observed.info["running"] = False
        observed.requests.clear()
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "off"
        assert status["desired"] == "off"
        assert status["current_url"] is None
        assert status["history_url"] == "https://business-fixture.trycloudflare.com/"
        assert observed.requests == []
        assert not any(resource["active"] for resource in runtime.resources.values())
        assert (
            panel.containers[0].state == "running"
        )  # CF never changed the business fixture.
    finally:
        panel.app.state.task_worker.stop()


def test_open_reuses_online_tunnel_and_only_refresh_rebuilds_it(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        first = panel.client.get("/api/assets/web/cf").json()["current_url"]
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert len(runtime.resources) == 1
        assert panel.client.get("/api/assets/web/cf").json()["current_url"] == first
        assert submit_cf(panel, "cf-refresh").status == "succeeded"
        assert len(runtime.resources) == 2
        assert sum(resource["active"] for resource in runtime.resources.values()) == 1
    finally:
        panel.app.state.task_worker.stop()


def test_origin_inspection_timeout_is_a_readable_error_not_server_failure(
    tmp_path, monkeypatch
):
    import subprocess

    panel = make_panel(tmp_path)
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[:3] == ["docker", "container", "inspect"]:
            raise subprocess.TimeoutExpired(command, 5)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    response = panel.client.get("/api/assets/web/cf/targets")
    assert response.status_code == 400
    assert "入口" in response.json()["detail"]
    assert panel.store.list_all() == []
    assert panel.client.get("/api/assets/web/cf").json()["state"] == "off"


def test_public_failure_cleans_tunnel_and_reports_stage(tmp_path, monkeypatch):
    import httpx

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    service = panel.app.state.cf_service
    service.runtime = runtime
    service.public_timeout = 0.03
    service.http_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                503 if request.url.host.endswith("trycloudflare.com") else 200,
                text="fixture",
            )
        )
    )
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "public_probe"
        assert status["current_url"] is None
        assert not any(resource["active"] for resource in runtime.resources.values())
        assert submit_cf(panel, "cf-disable").status == "succeeded"
    finally:
        panel.app.state.task_worker.stop()


def test_missing_dependency_is_reported_without_starting_resource(
    tmp_path, monkeypatch
):
    from app.services.cf_tunnels.models import CFError

    class MissingDependency(FakeTunnelRuntime):
        def start(self, *args, **kwargs):
            raise CFError("dependency", "缺少 cloudflared")

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = MissingDependency()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["error"]["stage"] == "dependency"
        assert status["current_url"] is None
        assert runtime.resources == {}
        assert submit_cf(panel, "cf-disable").status == "succeeded"
    finally:
        panel.app.state.task_worker.stop()


def test_failed_close_retains_owned_resource_for_retry(tmp_path, monkeypatch):
    from app.services.cf_tunnels.models import CFError

    class DelayedStop(FakeTunnelRuntime):
        failing = True

        def stop(self, handle, *, timeout=15):
            if self.failing:
                raise CFError("cleanup", "服务管理器繁忙")
            super().stop(handle, timeout=timeout)

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = DelayedStop()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert submit_cf(panel, "cf-disable").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "cleanup"
        assert status["current_url"] is None
        assert any(resource["active"] for resource in runtime.resources.values())
        runtime.failing = False
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert not any(resource["active"] for resource in runtime.resources.values())
    finally:
        panel.app.state.task_worker.stop()
