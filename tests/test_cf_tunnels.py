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
