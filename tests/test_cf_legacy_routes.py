"""Legacy tunnels share the public CF routes while keeping their external ownership."""

import subprocess
from types import SimpleNamespace

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
