"""Candidate tokens never authorize another project or arbitrary endpoints."""

import subprocess

import httpx
import pytest
import yaml

from tests.test_cf_project_entries import project_panel
from tests.test_cf_tunnels import attach_local_observation, make_panel


@pytest.mark.parametrize("path", ["/api/assets/web/cf", "/api/assets/web/cf/targets"])
def test_cf_reads_require_panel_login(tmp_path, path):
    panel = make_panel(tmp_path)
    panel.client.cookies.clear()
    assert panel.client.get(path).status_code == 401
    assert not panel.store.list_all()


def test_same_named_projects_do_not_share_candidate_credentials(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    first = panel.client.get("/api/assets/web/cf/targets").json()["targets"][0]
    other = tmp_path / "other" / "business"
    other.mkdir(parents=True)
    (other / "compose.yaml").write_text((panel.project / "compose.yaml").read_text())
    config = tmp_path / "config/objects/web.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["id"] = "other"
    settings["config"].update(project_dir=str(other), primary_container="other-web")
    (config.parent / "other.yaml").write_text(yaml.safe_dump(settings))
    container = panel.containers[0].model_copy(deep=True)
    container.id = "b" * 64
    container.name = "other-web"
    container.compose_working_dir = str(other)
    container.labels["com.docker.compose.project.working_dir"] = str(other)
    panel.containers.append(container)
    panel.app.state.rebuild_registry_runtime()
    original = subprocess.run

    def run(command, **kwargs):
        if (
            command[:3] == ["docker", "container", "inspect"]
            and command[-1] == "other-web"
        ):
            import json

            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {**observed.info, "id": container.id, "labels": container.labels}
                ),
                "",
            )
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    response = panel.client.post(
        "/api/bulk/actions/cf-create",
        json={
            "asset_ids": ["other"],
            "cf_target_map": {"other": {key: first[key] for key in ("id", "revision")}},
        },
    )
    assert response.status_code == 202
    assert response.json()["queued_count"] == 0
    assert not panel.store.list_all()
    forged = panel.client.post(
        "/api/bulk/actions/cf-create",
        json={
            "asset_ids": ["web"],
            "cf_target_map": {
                "web": {
                    "id": first["id"],
                    "revision": first["revision"],
                    "origin": "http://elsewhere.invalid",
                }
            },
        },
    )
    assert forged.status_code == 422
    assert not panel.store.list_all()


@pytest.mark.parametrize("case", ["external_redirect", "bad_certificate", "not_http"])
def test_bad_endpoints_do_not_create_tasks_or_follow_external_redirects(
    tmp_path, monkeypatch, case
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if case == "external_redirect":
            return httpx.Response(
                302, headers={"location": "https://elsewhere.invalid/login"}
            )
        if case == "bad_certificate":
            if request.url.scheme == "http":
                return httpx.Response(400)
            raise httpx.ConnectError("certificate verify failed", request=request)
        raise httpx.RemoteProtocolError("not HTTP", request=request)

    panel.app.state.cf_service.http_transport = httpx.MockTransport(respond)
    response = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    )
    assert response.json()["queued_count"] == 0
    assert not any("elsewhere.invalid" in request for request in requests)
    assert not panel.store.list_all()


def test_source_project_does_not_claim_an_unowned_listening_port(tmp_path, monkeypatch):
    panel, project, _ = project_panel(
        tmp_path, monkeypatch, cwd=tmp_path, metadata={"web_ui": {"port": "18182"}}
    )
    original = subprocess.run

    def run(command, **kwargs):
        if command[0] == "ss":
            return subprocess.CompletedProcess(
                command,
                0,
                'LISTEN 0 128 127.0.0.1:18182 0.0.0.0:* users:(("python",pid=777,fd=3))\n',
                "",
            )
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = panel.client.get(f"/api/assets/{project.object_id}/cf/targets")
    assert result.status_code == 200
    assert result.json()["targets"] == []
    response = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": [project.object_id]}
    )
    assert response.json()["queued_count"] == 0
    assert not panel.store.list_all()
    assert (tmp_path / "source/app.py").read_text() == "# source only\n"
