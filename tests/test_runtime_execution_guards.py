import subprocess

import pytest

from app.adapters.host_port_adapter import HostPortAdapter
from app.adapters.project_adapter import ProjectAdapter
from app.scanners.project_scanner import scan_projects
from app.services.capabilities import enrich_asset_capabilities
from app.models.assets import AssetSnapshot


def test_project_id_is_independent_of_other_same_named_projects(tmp_path):
    roots = [tmp_path / "a", tmp_path / "b"]
    for root in roots:
        (root / "demo").mkdir(parents=True)
        (root / "demo/pyproject.toml").touch()
    units = tmp_path / "units"
    units.mkdir()

    def scan(selected):
        return scan_projects(
            roots=selected, systemd_unit_roots=[units], command_runner=lambda *_: ""
        )

    first = scan(roots[:1])[0]
    both = scan(roots)
    assert first.object_id == next(
        a.object_id for a in both if a.metadata["path"] == first.metadata["path"]
    )


def test_host_plans_never_trust_fixed_port_targets():
    for owner, process in [("docker", "docker-proxy"), ("systemd", "python3")]:
        plan = HostPortAdapter(
            port="8317",
            owner_type=owner,
            process_name=process,
            pid=123,
            target_container_name="wrong",
            target_unit_name="wrong.service",
        ).plan_action("stop")
        assert all(
            command[:2] != ["docker", "stop"] and command[:2] != ["sudo", "systemctl"]
            for command in plan.commands
        )
        assert "app.tools.host_port_control" in plan.commands[0]


def test_project_runtime_requires_real_units_even_with_stale_supported_actions():
    asset = AssetSnapshot(
        object_id="p",
        category="project",
        name="p",
        status="present",
        supports_actions=["start"],
        metadata={"capabilities": {"runtime_control": {"enabled": True}}},
    )
    assert "start" not in enrich_asset_capabilities(asset).supports_actions
    asset = asset.model_copy(
        update={"metadata": {"service_units": ["a.service", "b.service"]}}
    )
    assert "start" in enrich_asset_capabilities(asset).supports_actions


def test_project_delete_is_checked_at_execution_time(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    plan = ProjectAdapter(
        project_dir=str(project), service_unit="fixture.service", service_scope="user"
    ).plan_action("full_delete")
    assert "app.services.project_lifecycle" in plan.commands[0]


class Units:
    def __init__(self, path, *, fail_disable=False):
        self.path = path
        self.calls = []
        self.fail_disable = fail_disable

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if "show" in command:
            return subprocess.CompletedProcess(
                command, 0, f"WorkingDirectory={self.path}\nLoadState=loaded\n", ""
            )
        if "is-active" in command:
            return subprocess.CompletedProcess(command, 3, "inactive\n", "")
        if "is-enabled" in command:
            return subprocess.CompletedProcess(command, 1, "disabled\n", "")
        code = 7 if self.fail_disable and "disable" in command else 0
        return subprocess.CompletedProcess(command, code, "", "")


def test_project_delete_stops_disables_then_removes_only_own_directory(tmp_path):
    from app.services.project_lifecycle import execute

    project = tmp_path / "project"
    project.mkdir()
    (project / "file").write_text("data")
    runner = Units(project)
    context = {
        "project_dir": str(project),
        "service_units": ["fixture.service"],
        "service_scope": "user",
    }
    execute(context, "full_delete", runner=runner)
    assert not project.exists()
    verbs = [
        c[2] for c in runner.calls if len(c) > 2 and c[:2] == ["systemctl", "--user"]
    ]
    assert verbs.index("stop") < verbs.index("disable")
    assert all("rm" not in c for c in runner.calls)


def test_project_delete_failure_retains_directory(tmp_path):
    from app.services.project_lifecycle import execute

    project = tmp_path / "project"
    project.mkdir()
    context = {
        "project_dir": str(project),
        "service_units": ["fixture.service"],
        "service_scope": "user",
    }
    with pytest.raises(RuntimeError):
        execute(context, "full_delete", runner=Units(project, fail_disable=True))
    assert project.is_dir()


def test_project_changed_unit_directory_blocks_all_mutations(tmp_path):
    from app.services.project_lifecycle import execute

    project = tmp_path / "project"
    project.mkdir()
    runner = Units(tmp_path / "other")
    with pytest.raises(ValueError):
        execute(
            {
                "project_dir": str(project),
                "service_units": ["fixture.service"],
                "service_scope": "user",
            },
            "full_delete",
            runner=runner,
        )
    assert project.is_dir()
    assert not any("stop" in c or "disable" in c for c in runner.calls)


def test_project_swapped_symlink_blocks_all_mutations(tmp_path):
    from app.services.project_lifecycle import execute

    project = tmp_path / "project"
    other = tmp_path / "other"
    other.mkdir()
    project.symlink_to(other)
    runner = Units(project)
    with pytest.raises(ValueError):
        execute(
            {
                "project_dir": str(project),
                "service_units": ["fixture.service"],
                "service_scope": "user",
            },
            "full_delete",
            runner=runner,
        )
    assert not runner.calls
    assert other.is_dir()


def test_host_changed_pid_start_time_is_not_signalled(tmp_path):
    from app.services.host_ownership import execute, process_identity

    proc = tmp_path / "proc"
    pid_dir = proc / "123"
    pid_dir.mkdir(parents=True)
    (pid_dir / "stat").write_text("123 (python3) S " + "0 " * 18 + "888 0\n")
    (pid_dir / "comm").write_text("python3\n")
    (pid_dir / "cgroup").write_text("0::/\n")
    identity = process_identity(123, proc_root=proc)
    assert identity["start_time"] == "888"
    snapshot = {
        "pid": 123,
        "process_name": "python3",
        "start_time": "777",
        "owner_type": "process",
        "port": "8317",
        "local_address": "127.0.0.1",
    }
    calls = []
    with pytest.raises(ValueError):
        execute(
            snapshot, "stop", proc_root=proc, runner=lambda *a, **kw: calls.append(a)
        )
    assert not calls


def test_project_directory_replaced_after_enqueue_is_preserved(tmp_path):
    from app.services.project_lifecycle import execute
    import json

    project = tmp_path / "project"
    project.mkdir()
    plan = ProjectAdapter(project_dir=str(project)).plan_action("full_delete")
    context = json.loads(plan.commands[0][3])
    project.rename(tmp_path / "original")
    project.mkdir()
    (project / "new").touch()
    with pytest.raises(ValueError, match="变化"):
        execute(
            context,
            "full_delete",
            runner=lambda *a, **kw: pytest.fail("unexpected command"),
        )
    assert (project / "new").exists()


def test_project_symlink_alias_does_not_change_identity(tmp_path):
    project = tmp_path / "real"
    project.mkdir()
    (project / "pyproject.toml").touch()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    units = tmp_path / "units"
    units.mkdir()

    def scan(path):
        return scan_projects(
            roots=[path], systemd_unit_roots=[units], command_runner=lambda *_: ""
        )[0]

    assert scan(project).object_id == scan(alias).object_id


def test_project_runtime_plan_preserves_each_unit_scope(tmp_path):
    adapter = ProjectAdapter(
        project_dir=str(tmp_path),
        service_targets=[
            {"name": "user.service", "scope": "user"},
            {"name": "system.service", "scope": "system"},
        ],
    )
    plan = adapter.plan_action("stop")
    assert ["systemctl", "--user", "stop", "user.service"] in plan.commands
    assert ["sudo", "systemctl", "stop", "system.service"] in plan.commands
    assert plan.requires_sudo


def test_project_delete_accepts_confirmed_script_inside_project(tmp_path):
    from app.services.project_lifecycle import execute

    project = tmp_path / "project"
    project.mkdir()
    base = Units(project)

    def run(command, **kwargs):
        if "show" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                f"WorkingDirectory={tmp_path}\nLoadState=loaded\nExecStart=/usr/bin/python3 {project}/server.py\n",
                "",
            )
        return base(command, **kwargs)

    execute(
        {
            "project_dir": str(project),
            "service_units": ["fixture.service"],
            "service_scope": "user",
        },
        "full_delete",
        runner=run,
    )
    assert not project.exists()


def test_project_execstart_suffix_collision_does_not_claim_other_service(tmp_path):
    from app.services.project_lifecycle import execute

    project = tmp_path / 'project'
    project.mkdir()
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0,
            f'LoadState=loaded\nWorkingDirectory=/elsewhere\nExecStart={{ path=/other{project}/server ; argv[]=/other{project}/server ; }}\n', '')

    with pytest.raises(ValueError, match='归属'):
        execute({'project_dir': str(project), 'service_units': ['other.service'],
                 'service_scope': 'user'}, 'full_delete', runner=runner)
    assert project.is_dir()
    assert not any('stop' in c for c in calls)


def test_project_scanner_ignores_name_and_path_substring_collisions(tmp_path):
    from app.scanners.project_scanner import _runtime_for_project

    project = tmp_path / 'project'
    assert _runtime_for_project(project, [
        {'service_unit': 'project.service', 'working_directory': '/elsewhere',
         'exec_start': f'/elsewhere{project}/server'},
    ]) == {}
