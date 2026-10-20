"""Exercise the detached entry point with a real child process, without systemd/CF."""

import json
from pathlib import Path
import subprocess
import sys

from app.services.cf_tunnels import runner


def make_cloudflared(tmp_path):
    binary = tmp_path / "cloudflared"
    binary.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, sys, time
# Cloudflared declares these flags on its tunnel subcommand, not globally.
if sys.argv[1:3] != ["tunnel", "--no-autoupdate"]:
    raise SystemExit(2)
root = pathlib.Path(os.environ["HOME"])
(root / "child.json").write_text(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("https://detached-fixture.trycloudflare.com", flush=True)
try:
    while True: time.sleep(.05)
finally:
    (root / "child-stopped").write_text("yes")
""")
    binary.chmod(0o700)
    return binary


def test_unverified_detached_runner_expires_and_reaps_its_child(tmp_path):
    binary = make_cloudflared(tmp_path)
    directory = tmp_path / "private-run"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(runner.__file__)),
            "--directory",
            str(directory),
            "--token",
            "fixture-token",
            "--binary",
            str(binary),
            "--origin",
            "http://127.0.0.1:18080",
            "--startup-seconds",
            "0.3",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 124, result.stderr
    assert (directory / "child-stopped").read_text() == "yes"
    assert json.loads((directory / "url.json").read_text()) == {
        "token": "fixture-token",
        "url": "https://detached-fixture.trycloudflare.com",
    }
    child = json.loads((directory / "child.json").read_text())
    assert child["env"]["HOME"] == str(directory)
    assert child["argv"][-1] == "http://127.0.0.1:18080"


def test_real_runtime_adapter_uses_transient_manager_and_stops_only_its_unit(
    tmp_path, monkeypatch
):
    import os
    from tests.test_cf_tunnels import attach_local_observation, make_panel, submit_cf

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    commands = tmp_path / "fake command path"
    commands.mkdir()
    binary = commands / "cloudflared"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{commands}:{os.environ['PATH']}")
    resources = {}
    original_run = subprocess.run

    def run(command, **kwargs):
        if command[:3] == ["systemctl", "--user", "show"]:
            unit = command[3]
            info = resources.get(
                unit,
                {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"},
            )
            return subprocess.CompletedProcess(
                command, 0, "\n".join(f"{k}={v}" for k, v in info.items()), ""
            )
        if command[0] == "systemd-run":
            options = {
                arg.split("=", 1)[0]: arg.split("=", 1)[1]
                for arg in command
                if arg.startswith("--") and "=" in arg
            }
            unit = options["--unit"]
            # Emulate the external manager accepting a transient service and its runner output.
            if unit in resources and resources[unit]["ActiveState"] == "active":
                return subprocess.CompletedProcess(
                    command, 1, "", "Unit already exists"
                )
            resources[unit] = {
                "LoadState": "loaded",
                "ActiveState": "active",
                "MainPID": "321",
                "InvocationID": "f" * 32,
                "Description": options["--description"],
                "Transient": "yes",
            }
            directory = Path(command[command.index("--directory") + 1])
            token = command[command.index("--token") + 1]
            (directory / "url.json").write_text(
                json.dumps(
                    {"token": token, "url": "https://manager-fixture.trycloudflare.com"}
                )
            )
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["systemctl", "--user", "stop"]:
            resources[command[3]].update(ActiveState="inactive", MainPID="0")
            return subprocess.CompletedProcess(command, 0, "", "")
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert (
            panel.client.get("/api/assets/web/cf").json()["current_url"]
            == "https://manager-fixture.trycloudflare.com/"
        )
        assert len(resources) == 1
        assert next(iter(resources.values()))["ActiveState"] == "active"
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert next(iter(resources.values()))["ActiveState"] == "inactive"
        assert panel.containers[0].state == "running"
    finally:
        panel.app.state.task_worker.stop()
