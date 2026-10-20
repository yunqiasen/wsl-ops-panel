"""Deterministic external-manager delays exercise the real runtime's time budgets."""

from pathlib import Path
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
            delay = self.delay
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
