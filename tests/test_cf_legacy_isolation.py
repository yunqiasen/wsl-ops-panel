"""Legacy blockers and unrelated units through normal project/Docker HTTP actions."""

import subprocess
from pathlib import Path

import pytest

from tests.test_cf_legacy import write_unit
from tests.test_cf_legacy_routes import attach_legacy
from tests.test_cf_project_entries import project_panel
from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


@pytest.mark.parametrize("case", ["masked", "ambiguous", "missing", "non_cf"])
def test_project_legacy_blockers_never_fall_through_to_new_or_foreground_tunnels(
    tmp_path, monkeypatch, case
):
    metadata = {} if case == "ambiguous" else {"capabilities": {"cf_tunnel": {"unit": "old-cf.service"}}}
    panel, project, _ = project_panel(tmp_path, monkeypatch, metadata=metadata)
    observed = attach_local_observation(panel, monkeypatch)
    roots = tmp_path / "units"
    roots.mkdir()
    if case == "masked":
        (roots / "old-cf.service").symlink_to("/dev/null")
    elif case != "missing":
        write_unit(
            roots, "old-cf.service", working_directory=project.metadata["path"],
            exec_start="/usr/bin/python app.py" if case == "non_cf" else
            "/usr/bin/cloudflared tunnel --url http://127.0.0.1:18182",
        )
        if case == "ambiguous":
            write_unit(
                roots, "second-cf.service", working_directory=project.metadata["path"],
                exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:18182",
            )
    originals = {path.name: (path.is_symlink(), path.read_bytes()) for path in roots.iterdir()}
    service = panel.app.state.cf_service
    service.legacy_unit_roots = [roots]
    service.runtime = FakeTunnelRuntime()
    commands = []
    external_run = subprocess.run

    def observe_commands(command, **kwargs):
        if command[0] in {"ss", "systemctl", "sudo", "journalctl"}:
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return external_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe_commands)
    status = panel.client.get(f"/api/assets/{project.object_id}/cf").json()
    assert status["capable"] is False
    assert status["management"] == ("external" if case == "masked" else "external_hint")
    assert status["reason"]
    assert status["current_url"] is None
    assert panel.client.get(f"/api/assets/{project.object_id}/cf/targets").status_code == 400
    for action in ("cf-create", "cf-refresh", "cf-disable"):
        response = panel.client.post(f"/api/bulk/actions/{action}", json={"asset_ids": [project.object_id]})
        assert response.status_code == 202
        assert response.json()["queued_count"] == 0
    assert not panel.store.list_all()
    assert service.runtime.resources == {}
    assert commands == []
    assert observed.requests == []
    assert {path.name: (path.is_symlink(), path.read_bytes()) for path in roots.iterdir()} == originals
    assert (Path(project.metadata["path"]) / "app.py").read_text() == "# source only\n"


def test_same_named_unrelated_old_resource_is_untouched_by_new_tunnel_on_and_off(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch)
    other = tmp_path / "another" / panel.project.name
    other.mkdir(parents=True)
    legacy.unit.write_text(legacy.unit.read_text().replace(str(panel.project), str(other)))
    legacy.info["WorkingDirectory"] = str(other)
    original = legacy.unit.read_bytes()
    service = panel.app.state.cf_service
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["management"] == "panel"
        assert status["current_url"] == "https://business-fixture.trycloudflare.com/"
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert not any(resource["active"] for resource in service.runtime.resources.values())
        assert legacy.info["ActiveState"] == "active"
        assert legacy.commands == []
        assert legacy.unit.read_bytes() == original
    finally:
        panel.app.state.task_worker.stop()


@pytest.mark.parametrize("location", ["unit_file", "live_dropin"])
def test_old_unit_with_extra_lifecycle_commands_is_not_controlled(
    tmp_path, monkeypatch, location
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch)
    # Stopping a unit can execute more than cloudflared's stop. Such hooks do
    # not belong to the panel's direct Quick Tunnel compatibility path.
    if location == "unit_file":
        legacy.unit.write_text(legacy.unit.read_text().replace("[Install]", "ExecStop=/usr/bin/business-stop-fixture\n\n[Install]"))
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["management"] == "external_hint"
        assert status["capable"] is False
        response = panel.client.post("/api/bulk/actions/cf-disable", json={"asset_ids": ["web"]})
        assert response.json()["queued_count"] == 0
        assert legacy.commands == []
    else:
        legacy.info["ExecStop"] = "/usr/bin/business-stop-fixture"
        panel.app.state.task_worker.start()
        try:
            assert submit_cf(panel, "cf-disable").status == "failed"
        finally:
            panel.app.state.task_worker.stop()
        assert not any("stop" in command for command in legacy.commands)
    assert legacy.info["ActiveState"] == "active"
    assert panel.containers[0].state == "running"
