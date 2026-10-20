"""Legacy tunnels share the public CF routes while keeping their external ownership."""

import subprocess
from types import SimpleNamespace

import pytest

from app.services.cf_tunnels.legacy import LegacyTunnelRuntime
from tests.test_cf_legacy import make_proc_root, make_systemd_mock, write_unit
from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


def attach_legacy(panel, tmp_path, monkeypatch, *, active=True):
    roots = tmp_path / "units"
    raw = "/usr/bin/cloudflared tunnel --url http://127.0.0.1:18080"
    unit = write_unit(
        roots,
        "explicit-old-cf.service",
        working_directory=str(panel.project),
        exec_start=raw,
    )
    info = {
        "LoadState": "loaded",
        "ActiveState": "active" if active else "inactive",
        "MainPID": "999" if active else "0",
        "InvocationID": "a" * 32 if active else "",
        "FragmentPath": str(unit),
        "WorkingDirectory": str(panel.project),
        "ExecStart": raw,
    }
    commands = []
    external = make_systemd_mock(
        {unit.name: info}, {unit.name: "https://legacy-fixture.trycloudflare.com"}
    )
    original_run = subprocess.run

    def run(command, **kwargs):
        if "systemctl" in command or "journalctl" in command:
            commands.append(command)
            return external(command, **kwargs)
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    service = panel.app.state.cf_service
    service.legacy_unit_roots = [roots]
    service.legacy_runtime = LegacyTunnelRuntime(make_proc_root(tmp_path))
    service.runtime = FakeTunnelRuntime()
    service.runtime.boot_id = service.legacy_runtime.boot_id
    return SimpleNamespace(info=info, commands=commands, unit=unit, roots=roots)


def test_explicit_old_unit_is_verified_and_controlled_without_creating_a_duplicate(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch)
    before = legacy.unit.read_bytes()
    status = panel.client.get("/api/assets/web/cf").json()
    assert status["management"] == "external"
    assert status["current_url"] is None
    assert legacy.commands == []
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["management"] == "external"
        assert status["state"] == "online"
        assert status["current_url"] == "https://legacy-fixture.trycloudflare.com/"
        assert panel.app.state.cf_service.runtime.resources == {}
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert legacy.info["ActiveState"] == "inactive"
        assert legacy.unit.read_bytes() == before
        assert not any(
            set(command) & {"enable", "disable", "unmask"}
            for command in legacy.commands
        )
        assert panel.containers[0].state == "running"
    finally:
        panel.app.state.task_worker.stop()


def test_failed_verification_leaves_an_already_external_tunnel_running(
    tmp_path, monkeypatch
):
    import httpx

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch)
    service = panel.app.state.cf_service
    service.public_timeout = 0.01
    service.http_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            503 if request.url.host.endswith("trycloudflare.com") else 200,
            text="fixture",
        )
    )
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        assert legacy.info["ActiveState"] == "active"
        assert panel.client.get("/api/assets/web/cf").json()["current_url"] is None
        assert service.runtime.resources == {}
    finally:
        panel.app.state.task_worker.stop()


def test_source_maintenance_withdraws_an_external_url_without_implicitly_stopping_its_unit(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch)
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        observed.info["running"] = False
        panel.app.state.cf_service.maintain()
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["current_url"] is None
        assert legacy.info["ActiveState"] == "active"
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert legacy.info["ActiveState"] == "inactive"
    finally:
        panel.app.state.task_worker.stop()


def test_explicit_close_of_an_unrecorded_old_unit_does_not_start_or_probe_the_source(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch)
    observed.info["running"] = False
    observed.requests.clear()
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert legacy.info["ActiveState"] == "inactive"
        assert not any("start" in command for command in legacy.commands)
        assert observed.requests == []
        assert panel.app.state.cf_service.runtime.resources == {}
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "off"
    finally:
        panel.app.state.task_worker.stop()


def test_script_only_clues_are_shown_without_enqueuing_the_foreground_script(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.cf_service.legacy_unit_roots = []
    script = panel.project / "scripts/cftunnel-start.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho foreground-script-fixture\n")
    status = panel.client.get("/api/assets/web/cf").json()
    assert status["management"] == "external_hint"
    assert status["capable"] is False
    assert status["current_url"] is None
    assert panel.client.get("/api/assets/web/cf/targets").status_code == 400
    result = panel.client.post("/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}).json()
    assert result["queued_count"] == 0
    assert panel.store.list_all() == []
    assert runtime.resources == {}
    assert observed.requests == []
    page = panel.client.get("/categories/docker")
    assert page.status_code == 200
    assert "外部线索" in page.text
    assert "缺少可核验 systemd 管理对象" in page.text


@pytest.mark.parametrize("failure", ["missing_id", "start_timeout", "registration_timeout", "start_oserror"])
def test_failed_legacy_registration_preserves_retryable_cleanup_without_guessing_an_instance(
    tmp_path, monkeypatch, failure
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    legacy = attach_legacy(panel, tmp_path, monkeypatch, active=False)
    external_run = subprocess.run

    registered = False
    broken = True

    def lose_registration(command, **kwargs):
        nonlocal registered
        if broken and registered and failure == "registration_timeout" and "show" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        result = external_run(command, **kwargs)
        if "systemctl" in command and "start" in command:
            registered = True
            if failure == "missing_id":
                legacy.info["InvocationID"] = ""
            elif failure == "start_timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            elif failure == "start_oserror":
                raise OSError("fixture private data")
        return result

    monkeypatch.setattr(subprocess, "run", lose_registration)
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["management"] == "external"
        assert status["current_url"] is None
        assert legacy.info["ActiveState"] == "active"
        assert not any("stop" in command for command in legacy.commands)
        assert panel.app.state.cf_service.runtime.resources == {}
        assert "private data" not in str(status)
        broken = False
        legacy.info["InvocationID"] = "b" * 32
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert legacy.info["ActiveState"] == "inactive"
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "off"
    finally:
        panel.app.state.task_worker.stop()
