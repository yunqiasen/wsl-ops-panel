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
