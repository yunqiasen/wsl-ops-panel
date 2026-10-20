"""Project CF ownership uses observed listeners and current service context."""

import os
from pathlib import Path
import subprocess

from app.models.assets import AssetSnapshot
from tests.test_cf_tunnels import make_panel, attach_local_observation


def project_panel(tmp_path, monkeypatch, *, metadata=None, cwd=None, unit=None):
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("# source only\n")
    project = AssetSnapshot(
        object_id="project__source",
        category="project",
        name="Source",
        status="present",
        metadata={"path": str(root), **(metadata or {})},
    )
    panel = make_panel(tmp_path, project_scanner=lambda: [project])
    attach_local_observation(panel, monkeypatch)
    proc = tmp_path / "proc" / "777"
    proc.mkdir(parents=True)
    (proc / "stat").write_text("777 (python) " + " ".join(["S"] + ["0"] * 18 + ["81"]))
    (proc / "comm").write_text("python\n")
    group = f"/user.slice/user-{os.getuid()}.slice/{unit or 'session.scope'}"
    (proc / "cgroup").write_text(f"0::{group}\n")
    (proc / "cwd").symlink_to(cwd or root)
    panel.app.state.cf_service.proc_root = proc.parent
    return panel, project, group


def test_current_service_owns_listener_even_if_worker_changed_directory(
    tmp_path, monkeypatch
):
    panel, project, group = project_panel(
        tmp_path,
        monkeypatch,
        cwd=Path("/tmp"),
        unit="source.service",
        metadata={"service_targets": [{"name": "source.service", "scope": "user"}]},
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
        if command[0] == "systemctl":
            assert "source.service" in command and "--user" in command
            return subprocess.CompletedProcess(
                command,
                0,
                f"ActiveState=active\nWorkingDirectory={project.metadata['path']}\nControlGroup={group}\nInvocationID=current-service-generation\n",
                "",
            )
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = panel.client.get("/api/assets/project__source/cf/targets")
    assert result.status_code == 200
    assert [target["origin"] for target in result.json()["targets"]] == [
        "http://127.0.0.1:18182"
    ]
    assert not panel.store.list_all()
