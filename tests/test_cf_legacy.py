"""Legacy CF tunnel adapter: inspect and control externally-managed systemd units.

Tests use the public adapter interface with real temporary unit files and a
real proc tree; only subprocess.run (external systemd/journalctl) is replaced.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from app.models.assets import AssetSnapshot
from app.services.cf_tunnels.legacy import LegacyTunnelRuntime, inspect_legacy
from app.services.cf_tunnels.models import CFError


# -- helpers -----------------------------------------------------------------


def make_asset(project_dir: str | Path, **metadata) -> AssetSnapshot:
    base: dict = {"project_dir": str(project_dir)}
    base.update(metadata)
    return AssetSnapshot(
        object_id="test",
        category="project",
        name="test",
        status="ok",
        metadata=base,
    )


def write_unit(
    unit_root: Path,
    name: str,
    *,
    working_directory: str,
    exec_start: str,
) -> Path:
    unit_root.mkdir(parents=True, exist_ok=True)
    path = unit_root / name
    path.write_text(
        f"[Unit]\nDescription=Legacy CF Tunnel\n\n"
        f"[Service]\nType=simple\n"
        f"WorkingDirectory={working_directory}\n"
        f"ExecStart={exec_start}\n\n"
        f"[Install]\nWantedBy=multi-user.target\n"
    )
    return path


def make_proc_root(tmp_path: Path) -> Path:
    proc = tmp_path / "proc"
    (proc / "sys" / "kernel" / "random").mkdir(parents=True)
    (proc / "sys" / "kernel" / "random" / "boot_id").write_text("test-boot-id\n")
    return proc


def _format_exec_start_for_show(raw: str) -> str:
    """Convert raw ExecStart to systemctl show { path=... ; argv[]=... } format."""
    if not raw or raw.startswith("{"):
        return raw
    parts = raw.split(None, 1)
    path = parts[0]
    return f"{{ path={path} ; argv[]={raw} ; ignore_errors=no }}"


def make_systemd_mock(
    units: dict[str, dict[str, str]],
    journal_urls: dict[str, str] | None = None,
):
    """Return a subprocess.run replacement for systemctl / journalctl."""
    journal_urls = journal_urls or {}

    def run(command, **kwargs):
        cmd = list(command)

        # journalctl
        if cmd[0] == "journalctl" or (
            len(cmd) >= 3 and cmd[0] == "sudo" and cmd[2] == "journalctl"
        ):
            if cmd[0] == "sudo":
                args = cmd[3:]
            else:
                args = cmd[1:]
            unit = ""
            for a in args:
                if a.startswith("--unit="):
                    unit = a.split("=", 1)[1]
            url = journal_urls.get(unit)
            if url:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    f"INFO tunnel registered tunnel-url={url}\n"
                    f"INF Registered tunnel connection\n",
                    "",
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        # systemctl
        if cmd[0] == "systemctl" and len(cmd) > 1 and cmd[1] == "--user":
            rest = cmd[2:]
        elif (
            len(cmd) >= 3
            and cmd[0] == "sudo"
            and cmd[1] == "-n"
            and cmd[2] == "systemctl"
        ):
            rest = cmd[3:]
        elif cmd[0] == "systemctl":
            rest = cmd[1:]
        else:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if not rest:
            return subprocess.CompletedProcess(cmd, 0, "", "")

        action = rest[0]

        if action == "show":
            unit = rest[1] if len(rest) > 1 else ""
            info = units.get(unit)
            if info is None:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    "LoadState=not-found\nActiveState=inactive\nMainPID=0\n",
                    "",
                )
            lines = []
            for k, v in info.items():
                if k == "ExecStart" and not v.startswith("{"):
                    v = _format_exec_start_for_show(v)
                lines.append(f"{k}={v}")
            return subprocess.CompletedProcess(cmd, 0, "\n".join(lines) + "\n", "")

        if action == "start":
            unit = rest[1] if len(rest) > 1 else ""
            if unit in units:
                units[unit]["ActiveState"] = "active"
                units[unit]["MainPID"] = "999"
                if not units[unit].get("InvocationID"):
                    units[unit]["InvocationID"] = "a" * 32
            return subprocess.CompletedProcess(cmd, 0, "", "")

        if action == "stop":
            unit = rest[1] if len(rest) > 1 else ""
            if unit in units:
                units[unit]["ActiveState"] = "inactive"
                units[unit]["MainPID"] = "0"
            return subprocess.CompletedProcess(cmd, 0, "", "")

        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run


# -- inspect_legacy: basic --------------------------------------------------


def test_inspect_returns_none_when_no_legacy_clues(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("hello")
    asset = make_asset(project)
    assert inspect_legacy(asset, unit_roots=[tmp_path / "units"]) is None


def test_inspect_finds_direct_cloudflared_unit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["unit"] == "legacy-cf.service"
    assert result["scope"] == "system"
    assert result["origin"] == "http://127.0.0.1:8080"
    assert result["load_state"] == "loaded"
    assert result["fragment_path"]


def test_inspect_external_hint_for_script_only(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "scripts").mkdir()
    (project / "scripts" / "cftunnel-start.sh").write_text(
        "#!/bin/sh\ncloudflared tunnel --url http://localhost:8080\n"
    )
    result = inspect_legacy(make_asset(project), unit_roots=[tmp_path / "units"])
    assert result is not None
    assert result["management"] == "external_hint"
    assert "reason" in result


def test_inspect_preserves_masked_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    unit_root.mkdir(parents=True)
    (unit_root / "legacy-cf.service").symlink_to("/dev/null")
    asset = make_asset(
        project,
        capabilities={"cf_tunnel": {"unit": "legacy-cf.service"}},
    )
    result = inspect_legacy(asset, unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["load_state"] == "masked"
    assert result["unit"] == "legacy-cf.service"


def test_inspect_ignores_unrelated_same_name_unit(tmp_path: Path) -> None:
    project = tmp_path / "myproject"
    project.mkdir()
    other = tmp_path / "otherproject"
    other.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(other),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    assert inspect_legacy(make_asset(project), unit_roots=[unit_root]) is None


def test_inspect_finds_unit_via_explicit_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "my-tunnel.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:3000",
    )
    asset = make_asset(
        project,
        capabilities={"cf_tunnel": {"cftunnel_unit": "my-tunnel.service"}},
    )
    result = inspect_legacy(asset, unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["unit"] == "my-tunnel.service"
    assert result["origin"] == "http://127.0.0.1:3000"


def test_inspect_external_hint_for_shell_script_unit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/bin/sh -c 'cloudflared tunnel --url ${TUNNEL_URL}'",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external_hint"
    assert "reason" in result


def test_inspect_multiple_units_raises(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "tunnel-a.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    write_unit(
        unit_root,
        "tunnel-b.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    with pytest.raises(CFError) as exc_info:
        inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert exc_info.value.stage == "legacy_ambiguous"


# -- inspect_legacy: Fix 2 — ExecStart parsing ------------------------------


def test_inspect_rejects_echo_as_cloudflared(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/bin/echo cloudflared --url http://127.0.0.1:8080",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_accepts_url_equals_format(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url=http://127.0.0.1:9090",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["origin"] == "http://127.0.0.1:9090"


def test_inspect_rejects_credentials_in_url(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://user:pass@127.0.0.1:8080",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_rejects_non_http_scheme(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url tcp://127.0.0.1:8080",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_rejects_public_ip_origin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://8.8.8.8:8080",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_accepts_lan_origin(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://10.0.0.5:8080",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["origin"] == "http://10.0.0.5:8080"


# -- inspect_legacy: Fix 3 — _find_script_clues ------------------------------


def test_inspect_hint_for_known_script_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "scripts").mkdir()
    (project / "scripts" / "cftunnel-start.sh").write_text("#!/bin/sh\n")
    result = inspect_legacy(make_asset(project), unit_roots=[tmp_path / "units"])
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_hint_for_known_domain_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "logs").mkdir()
    (project / "logs" / "cftunnel-domain.txt").write_text("abc-def.trycloudflare.com")
    result = inspect_legacy(make_asset(project), unit_roots=[tmp_path / "units"])
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_hint_for_metadata_start_script(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "custom-start.sh").write_text("#!/bin/sh\n")
    result = inspect_legacy(
        make_asset(project, start_script="custom-start.sh"),
        unit_roots=[tmp_path / "units"],
    )
    assert result is not None
    assert result["management"] == "external_hint"


def test_inspect_no_hint_for_arbitrary_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "notes.txt").write_text("cloudflared is great, trycloudflare.com")
    result = inspect_legacy(make_asset(project), unit_roots=[tmp_path / "units"])
    assert result is None


# -- inspect_legacy: Fix 4 — ambiguity & priority ----------------------------


def test_inspect_ambiguous_same_name_system_and_user(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sys_root = tmp_path / "etc" / "systemd" / "system"
    usr_root = tmp_path / "home" / ".config" / "systemd" / "user"
    write_unit(
        sys_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    write_unit(
        usr_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    asset = make_asset(
        project,
        capabilities={"cf_tunnel": {"unit": "legacy-cf.service"}},
    )
    with pytest.raises(CFError) as exc_info:
        inspect_legacy(asset, unit_roots=[sys_root, usr_root])
    assert exc_info.value.stage == "legacy_ambiguous"


def test_inspect_etc_overrides_lib(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    etc_root = tmp_path / "etc" / "systemd" / "system"
    lib_root = tmp_path / "lib" / "systemd" / "system"
    write_unit(
        etc_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    write_unit(
        lib_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:9999",
    )
    result = inspect_legacy(make_asset(project), unit_roots=[etc_root, lib_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["origin"] == "http://127.0.0.1:8080"
    assert result["fragment_path"] == str(etc_root / "legacy-cf.service")


def test_inspect_masked_etc_overrides_lib_normal(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    etc_root = tmp_path / "etc" / "systemd" / "system"
    lib_root = tmp_path / "lib" / "systemd" / "system"
    etc_root.mkdir(parents=True)
    (etc_root / "legacy-cf.service").symlink_to("/dev/null")
    write_unit(
        lib_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    asset = make_asset(
        project,
        capabilities={"cf_tunnel": {"unit": "legacy-cf.service"}},
    )
    result = inspect_legacy(asset, unit_roots=[etc_root, lib_root])
    assert result is not None
    assert result["management"] == "external"
    assert result["load_state"] == "masked"


def test_inspect_explicit_unit_wrong_workdir_hint(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(other),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080",
    )
    asset = make_asset(
        project,
        capabilities={"cf_tunnel": {"unit": "legacy-cf.service"}},
    )
    result = inspect_legacy(asset, unit_roots=[unit_root])
    assert result is not None
    assert result["management"] == "external_hint"
    assert "工作目录" in result["reason"]


# -- LegacyTunnelRuntime: lifecycle -----------------------------------------


def _make_unit_info(
    project: Path,
    unit_root: Path,
    exec_start: str = "/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    **extra,
) -> dict[str, str]:
    info = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "MainPID": "0",
        "InvocationID": "",
        "Description": "Legacy CF Tunnel",
        "FragmentPath": str(unit_root / "legacy-cf.service"),
        "WorkingDirectory": str(project),
        "ExecStart": exec_start,
        "Transient": "no",
    }
    info.update(extra)
    return info


def test_start_observe_stop_with_matching_invocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    legacy = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert legacy and legacy["management"] == "external"

    units = {
        "legacy-cf.service": _make_unit_info(project, unit_root),
    }
    journal_urls = {"legacy-cf.service": "https://abc-def.trycloudflare.com"}
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units, journal_urls))

    rt = LegacyTunnelRuntime(proc_root=proc_root)
    handle = rt.start(
        "owner1", "tok1", "http://127.0.0.1:8080", timeout=10, legacy=legacy
    )
    assert handle["kind"] == "legacy"
    assert handle["unit"] == "legacy-cf.service"
    assert handle["scope"] == "system"
    assert handle["boot_id"] == "test-boot-id"
    assert handle["invocation_id"]
    assert handle["origin"] == "http://127.0.0.1:8080"
    assert handle["project_dir"] == str(project)
    assert handle["fragment_path"]

    observed = rt.observe(handle, timeout=5)
    assert observed["active"] is True
    assert observed["url"] == "https://abc-def.trycloudflare.com"

    rt.activate(handle, timeout=5)

    rt.stop(handle, timeout=15)
    assert units["legacy-cf.service"]["ActiveState"] == "inactive"


def test_start_rejects_origin_mismatch(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    legacy = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert legacy

    units = {"legacy-cf.service": _make_unit_info(project, unit_root)}
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units))

    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError) as exc:
        rt.start("o", "t", "http://127.0.0.1:9999", timeout=10, legacy=legacy)
    assert exc.value.stage == "legacy_origin"


def test_masked_blocks_start(tmp_path: Path) -> None:
    proc_root = make_proc_root(tmp_path)
    legacy = {
        "management": "external",
        "unit": "legacy-cf.service",
        "scope": "system",
        "load_state": "masked",
        "fragment_path": "/etc/systemd/system/legacy-cf.service",
        "project_dir": str(tmp_path),
    }
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError) as exc:
        rt.start("o", "t", "http://127.0.0.1:8080", timeout=10, legacy=legacy)
    assert exc.value.stage == "legacy_masked"


def test_start_reuses_active_invocation(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    legacy = inspect_legacy(make_asset(project), unit_roots=[unit_root])

    inv = "b" * 32
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="123",
            InvocationID=inv,
        ),
    }
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units))

    rt = LegacyTunnelRuntime(proc_root=proc_root)
    handle = rt.start("o", "t", "http://127.0.0.1:8080", timeout=10, legacy=legacy)
    assert handle["invocation_id"] == inv
    assert units["legacy-cf.service"]["MainPID"] == "123"


def test_observe_rejects_changed_invocation(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": "old" + "0" * 29,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="456",
            InvocationID="new" + "0" * 29,
        ),
    }
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units))
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError) as exc:
        rt.observe(handle, timeout=5)
    assert exc.value.stage == "legacy_identity"


def test_stop_does_not_disable_or_unmask(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "c" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="789",
            InvocationID=inv,
        ),
    }
    seen: list[list[str]] = []
    mock = make_systemd_mock(units)

    def tracking_run(command, **kwargs):
        seen.append(list(command))
        return mock(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracking_run)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    rt.stop(handle, timeout=15)

    flat = " ".join(" ".join(c) for c in seen)
    assert "stop" in flat
    assert "disable" not in flat
    assert "unmask" not in flat
    assert units["legacy-cf.service"]["ActiveState"] == "inactive"


def test_total_timeout_enforced(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    legacy = inspect_legacy(make_asset(project), unit_roots=[unit_root])

    def slow_run(command, **kwargs):
        t = kwargs.get("timeout")
        if t is not None and t < 0.5:
            raise subprocess.TimeoutExpired(command, t)
        return make_systemd_mock({})(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow_run)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    t0 = time.monotonic()
    with pytest.raises(CFError):
        rt.start("o", "t", "http://127.0.0.1:8080", timeout=0.01, legacy=legacy)
    assert time.monotonic() - t0 < 1.0


def test_observe_returns_inactive_when_boot_id_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    proc_root = make_proc_root(tmp_path)
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "different-boot-id",
        "invocation_id": "d" * 32,
        "fragment_path": "/etc/systemd/system/legacy-cf.service",
        "project_dir": str(tmp_path / "project"),
        "origin": "http://127.0.0.1:8080",
    }
    monkeypatch.setattr(subprocess, "run", make_systemd_mock({}))
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    result = rt.observe(handle, timeout=5)
    assert result == {"active": False, "url": None}


def test_user_scope_uses_systemctl_user(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    user_root = tmp_path / "systemd" / "user"
    write_unit(
        user_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    legacy = inspect_legacy(make_asset(project), unit_roots=[user_root])
    assert legacy and legacy["scope"] == "user"

    units = {
        "legacy-cf.service": _make_unit_info(project, user_root),
    }
    seen: list[list[str]] = []
    mock = make_systemd_mock(units)

    def tracking_run(command, **kwargs):
        seen.append(list(command))
        return mock(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracking_run)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    handle = rt.start("o", "t", "http://127.0.0.1:8080", timeout=10, legacy=legacy)
    assert handle["scope"] == "user"
    systemctl_cmds = [c for c in seen if c[0] == "systemctl"]
    assert systemctl_cmds, f"no systemctl commands in {seen}"
    assert systemctl_cmds[0][1] == "--user"


# -- Fix 1: stop() InvocationID check ----------------------------------------


def test_stop_rejects_changed_invocation(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "c" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="789",
            InvocationID="d" * 32,  # different!
        ),
    }
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units))
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError) as exc:
        rt.stop(handle, timeout=15)
    assert exc.value.stage == "legacy_identity"
    # Unit must NOT have been stopped.
    assert units["legacy-cf.service"]["ActiveState"] == "active"


# -- Fix 5: stop() activating/deactivating & deadline ------------------------


def test_stop_activating_mainpid_zero(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "f" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="activating",
            MainPID="0",
            InvocationID=inv,
        ),
    }
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units))
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    rt.stop(handle, timeout=15)
    assert units["legacy-cf.service"]["ActiveState"] == "inactive"


def test_stop_zero_timeout_no_extension(tmp_path: Path, monkeypatch) -> None:
    """timeout=0 must raise CFError immediately, no max(0.01,...) extension."""
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "g" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    called: list[list[str]] = []
    mock = make_systemd_mock({})

    def tracking_run(command, **kwargs):
        called.append(list(command))
        return mock(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracking_run)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError):
        rt.stop(handle, timeout=0)
    assert len(called) == 0  # No subprocess calls should have been made.


def test_stop_verifies_stopped_before_success(tmp_path: Path, monkeypatch) -> None:
    """After issuing stop, must observe confirmation — not claim success blindly."""
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "h" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    # Unit stays active even after stop — custom mock that ignores stop.
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="789",
            InvocationID=inv,
        ),
    }

    def stubborn_run(command, **kwargs):
        cmd = list(command)
        # Detect systemctl show
        is_show = any("--property=" in c for c in cmd)
        is_stop = False
        for i, c in enumerate(cmd):
            if c == "stop" and i > 0:
                is_stop = True
                break
        if is_stop:
            # Stop succeeds but unit stubbornly stays active
            return subprocess.CompletedProcess(cmd, 0, "", "")
        # Show returns current state (still active)
        if is_show:
            info = units.get("legacy-cf.service", {})
            lines = []
            for k, v in info.items():
                if k == "ExecStart" and not v.startswith("{"):
                    v = _format_exec_start_for_show(v)
                lines.append(f"{k}={v}")
            return subprocess.CompletedProcess(cmd, 0, "\n".join(lines) + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", stubborn_run)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError) as exc:
        rt.stop(handle, timeout=15)
    assert exc.value.stage == "legacy_cleanup"


# -- Fix 6: observe() URL & state preservation ------------------------------


def test_observe_preserves_enabled_masked(tmp_path: Path, monkeypatch) -> None:
    """observe must not issue enable/disable/unmask commands."""
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "i" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="999",
            InvocationID=inv,
        ),
    }
    journal_urls = {"legacy-cf.service": "https://abc-def.trycloudflare.com"}
    seen: list[list[str]] = []
    mock = make_systemd_mock(units, journal_urls)

    def tracking_run(command, **kwargs):
        seen.append(list(command))
        return mock(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracking_run)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    result = rt.observe(handle, timeout=5)
    assert result["active"] is True
    flat = " ".join(" ".join(c) for c in seen)
    assert "enable" not in flat
    assert "disable" not in flat
    assert "unmask" not in flat


def test_observe_rejects_invalid_journal_url(tmp_path: Path, monkeypatch) -> None:
    """Invalid journal URLs must yield url=None, not raise."""
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "j" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="999",
            InvocationID=inv,
        ),
    }
    # Invalid URL in journal — not a trycloudflare.com domain
    journal_urls = {"legacy-cf.service": "https://evil.example.com/steal"}
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units, journal_urls))
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    result = rt.observe(handle, timeout=5)
    assert result["active"] is True
    assert result["url"] is None


def test_observe_no_url_from_old_invocation(tmp_path: Path, monkeypatch) -> None:
    """Journal is scoped to current InvocationID — old logs don't prove online."""
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    inv = "k" * 32
    handle = {
        "kind": "legacy",
        "owner": "o",
        "token": "t",
        "unit": "legacy-cf.service",
        "scope": "system",
        "boot_id": "test-boot-id",
        "invocation_id": inv,
        "fragment_path": str(unit_root / "legacy-cf.service"),
        "project_dir": str(project),
        "origin": "http://127.0.0.1:8080",
    }
    units = {
        "legacy-cf.service": _make_unit_info(
            project,
            unit_root,
            ActiveState="active",
            MainPID="999",
            InvocationID=inv,
        ),
    }
    # Journal returns empty (no entries for this invocation)
    monkeypatch.setattr(subprocess, "run", make_systemd_mock(units))
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    result = rt.observe(handle, timeout=5)
    assert result["active"] is True
    assert result["url"] is None


# -- error.handle on start failure ------------------------------------------


def test_start_failed_sets_error_handle(tmp_path: Path, monkeypatch) -> None:
    """If start succeeds but post-start verification fails, CFError gets .handle."""
    project = tmp_path / "project"
    project.mkdir()
    proc_root = make_proc_root(tmp_path)
    unit_root = tmp_path / "units"
    write_unit(
        unit_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8080",
    )
    legacy = inspect_legacy(make_asset(project), unit_roots=[unit_root])
    assert legacy

    units = {
        "legacy-cf.service": _make_unit_info(project, unit_root),
    }
    mock = make_systemd_mock(units)
    call_count = 0

    def start_then_fail(command, **kwargs):
        nonlocal call_count
        result = mock(command, **kwargs)
        cmd = list(command)
        # After start command, make the next show return inactive
        if any("start" in str(c) for c in cmd):
            call_count += 1
            # Simulate start succeeded but unit never became active
            units["legacy-cf.service"]["ActiveState"] = "failed"
            units["legacy-cf.service"]["MainPID"] = "0"
            units["legacy-cf.service"]["InvocationID"] = ""
        return result

    monkeypatch.setattr(subprocess, "run", start_then_fail)
    rt = LegacyTunnelRuntime(proc_root=proc_root)
    with pytest.raises(CFError) as exc:
        rt.start("o", "t", "http://127.0.0.1:8080", timeout=10, legacy=legacy)
    assert hasattr(exc.value, "handle")
    assert exc.value.handle["unit"] == "legacy-cf.service"
