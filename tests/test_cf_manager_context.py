"""A system service supplies no login-shell session environment to CF tasks."""

import os
from pathlib import Path
import socket
import subprocess
from tempfile import TemporaryDirectory

import pytest

from app.services.cf_tunnels import runtime as runtime_module
from tests.test_cf_deadlines import DelayedManager
from tests.test_cf_legacy_routes import attach_legacy
from tests.test_cf_tunnels import attach_local_observation, make_panel, submit_cf


@pytest.mark.parametrize("kind", ["managed", "legacy_user"])
@pytest.mark.parametrize("configured", [False, True])
def test_cf_tasks_find_only_their_own_existing_user_manager_without_mutating_environment(
    tmp_path, monkeypatch, kind, configured
):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    if kind == "managed":
        manager = DelayedManager(tmp_path, monkeypatch)
        manager.delay = manager.next_delay = 0
    else:
        manager = attach_legacy(panel, tmp_path, monkeypatch, unit_root=tmp_path / "user")
    external_run = subprocess.run
    with TemporaryDirectory(prefix="cf-session-") as directory:
        root = Path(directory)
        user_dir = root / ("explicit" if configured else str(os.geteuid()))
        user_dir.mkdir(mode=0o700)
        bus = socket.socket(socket.AF_UNIX)
        bus.bind(str(user_dir / "bus"))
        monkeypatch.setattr(runtime_module, "USER_RUNTIME_ROOT", root, raising=False)
        if configured:
            monkeypatch.setenv("XDG_RUNTIME_DIR", str(user_dir))
            monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={user_dir}/bus")
        before_environment = dict(os.environ)
        contexts = []

        def require_session(command, **kwargs):
            if command[0] in {"systemctl", "systemd-run", "journalctl"}:
                env = kwargs.get("env", os.environ)
                contexts.append(dict(env))
                if env.get("XDG_RUNTIME_DIR") != str(user_dir) or env.get("DBUS_SESSION_BUS_ADDRESS") != f"unix:path={user_dir}/bus":
                    return subprocess.CompletedProcess(command, 1, "", "Failed to connect to bus: No medium found")
            return external_run(command, **kwargs)

        monkeypatch.setattr(subprocess, "run", require_session)
        panel.app.state.task_worker.start()
        try:
            assert submit_cf(panel, "cf-create").status == "succeeded"
            assert panel.client.get("/api/assets/web/cf").json()["state"] == "online"
            assert submit_cf(panel, "cf-disable").status == "succeeded"
            assert manager.info["ActiveState"] == "inactive"
            assert len(contexts) >= 4
            assert dict(os.environ) == before_environment
        finally:
            panel.app.state.task_worker.stop()
            bus.close()
