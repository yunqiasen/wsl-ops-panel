"""Deterministic external-manager delays exercise the real runtime's time budgets."""

from pathlib import Path
import json
import subprocess

import pytest

from app.services.cf_tunnels.models import CFError
from app.services.cf_tunnels.runtime import SystemdTunnelRuntime


class DelayedManager:
    def __init__(self, tmp_path, monkeypatch):
        self.clock = 0.0
        self.info = {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "MainPID": "0",
        }
        self.timeouts = []
        self.command_delays = {}
        self.delay = 5
        self.next_delay = 3
        binary = tmp_path / "cloudflared"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        monkeypatch.setattr(
            "app.services.cf_tunnels.runtime.shutil.which", lambda name: str(binary)
        )
        monkeypatch.setattr(
            "app.services.cf_tunnels.runtime.monotonic", lambda: self.clock
        )
        monkeypatch.setattr("app.services.cf_tunnels.runtime.sleep", self.sleep)
        original_run = subprocess.run

        def run(command, **kwargs):
            if command[0] not in {"systemctl", "systemd-run"}:
                return original_run(command, **kwargs)
            limit = kwargs["timeout"]
            self.timeouts.append(limit)
            delay = self.command_delays.get(tuple(command[:3]), self.delay)
            self.delay = self.next_delay
            self.clock += min(delay, limit)
            if command[0] == "systemd-run":
                description = next(
                    value.split("=", 1)[1]
                    for value in command
                    if value.startswith("--description=")
                )
                self.info = {
                    "LoadState": "loaded",
                    "ActiveState": "active",
                    "MainPID": "321",
                    "InvocationID": "f" * 32,
                    "Description": description,
                    "Transient": "yes",
                }
                self.directory = Path(command[command.index("--directory") + 1])
                token = command[command.index("--token") + 1]
                (self.directory / "url.json").write_text(
                    json.dumps(
                        {
                            "token": token,
                            "url": "https://deadline-fixture.trycloudflare.com",
                        }
                    )
                )
            if delay > limit:
                raise subprocess.TimeoutExpired(command, limit)
            if command[:3] == ["systemctl", "--user", "stop"]:
                self.info.update(ActiveState="inactive", MainPID="0")
            return subprocess.CompletedProcess(
                command,
                0,
                "\n".join(f"{key}={value}" for key, value in self.info.items()),
                "",
            )

        monkeypatch.setattr(subprocess, "run", run)

    def sleep(self, seconds):
        self.clock += seconds


def test_registration_obeys_one_total_budget_across_inspect_start_and_register(
    tmp_path, monkeypatch
):
    manager = DelayedManager(tmp_path, monkeypatch)
    runtime = SystemdTunnelRuntime(tmp_path / "cf")
    with pytest.raises(CFError) as error:
        runtime.start("a" * 64, "fixture-token", "http://127.0.0.1:18080", timeout=10)
    assert error.value.stage == "runtime"
    assert manager.clock <= 10


def test_launch_registration_timeout_reaps_the_resource_already_created(
    tmp_path, monkeypatch
):
    from tests.test_cf_tunnels import attach_local_observation, make_panel, submit_cf

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    manager = DelayedManager(tmp_path, monkeypatch)
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "runtime"
        assert status["current_url"] is None
        assert manager.info["ActiveState"] == "inactive"
        assert manager.clock <= 25  # launch budget plus at most fifteen for cleanup
        assert panel.containers[0].state == "running"
    finally:
        panel.app.state.task_worker.stop()


def test_close_cancels_an_activating_owned_unit_even_before_its_pid_is_registered(
    tmp_path, monkeypatch
):
    manager = DelayedManager(tmp_path, monkeypatch)
    manager.delay = manager.next_delay = 0
    runtime = SystemdTunnelRuntime(tmp_path / "cf")
    handle = runtime.start(
        "a" * 64, "fixture-token", "http://127.0.0.1:18080", timeout=10
    )
    manager.info.update(ActiveState="activating", MainPID="0")
    runtime.stop(handle, timeout=15)
    assert manager.info["ActiveState"] == "inactive"


def test_final_runtime_activation_is_inside_the_total_open_budget(
    tmp_path, monkeypatch
):
    import httpx
    from tests.test_cf_tunnels import attach_local_observation, make_panel, submit_cf

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    manager = DelayedManager(tmp_path, monkeypatch)
    manager.delay = manager.next_delay = 0
    monkeypatch.setattr("app.services.cf_tunnels.monotonic", lambda: manager.clock)
    monkeypatch.setattr("app.services.cf_tunnels.sleep", manager.sleep)
    monkeypatch.setattr(
        "app.services.cf_tunnels.origins.monotonic", lambda: manager.clock
    )
    service = panel.app.state.cf_service
    # Stretch the nested phase to exercise the independent hard outer budget.
    service.public_timeout = 120

    def respond(request):
        if request.url.host.endswith("trycloudflare.com"):
            manager.clock = 119
            manager.delay = manager.next_delay = 3
        return httpx.Response(200, text="business fixture")

    service.http_transport = httpx.MockTransport(respond)
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["current_url"] is None
        assert manager.info["ActiveState"] == "inactive"
        assert manager.clock <= 135
    finally:
        panel.app.state.task_worker.stop()


def test_close_timeout_is_bounded_and_keeps_only_the_owned_resource_for_retry(
    tmp_path, monkeypatch
):
    from tests.test_cf_tunnels import attach_local_observation, make_panel, submit_cf

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    manager = DelayedManager(tmp_path, monkeypatch)
    manager.delay = manager.next_delay = 0
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        manager.command_delays[("systemctl", "--user", "stop")] = 20
        started = manager.clock
        assert submit_cf(panel, "cf-disable").status == "failed"
        assert manager.clock - started <= 15
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "cleanup"
        assert status["current_url"] is None
        assert manager.info["ActiveState"] == "active"
        manager.command_delays.clear()
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert manager.info["ActiveState"] == "inactive"
    finally:
        panel.app.state.task_worker.stop()


@pytest.mark.parametrize(
    "change", ["missing_binary", "unreadable_url", "different_invocation"]
)
def test_close_uses_runtime_ownership_not_binary_or_domain_files(
    tmp_path, monkeypatch, change
):
    from tests.test_cf_tunnels import attach_local_observation, make_panel, submit_cf

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    manager = DelayedManager(tmp_path, monkeypatch)
    manager.delay = manager.next_delay = 0
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        observed.requests.clear()
        if change == "missing_binary":
            (tmp_path / "cloudflared").unlink()
        elif change == "unreadable_url":
            (manager.directory / "url.json").chmod(0)
        else:
            manager.info["InvocationID"] = "e" * 32
        task = submit_cf(panel, "cf-disable")
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["current_url"] is None
        if change == "different_invocation":
            assert task.status == "failed"
            assert status["error"]["stage"] == "runtime_identity"
            assert manager.info["ActiveState"] == "active"
        else:
            assert task.status == "succeeded"
            assert manager.info["ActiveState"] == "inactive"
        assert observed.requests == []
        assert panel.containers[0].state == "running"
    finally:
        panel.app.state.task_worker.stop()
