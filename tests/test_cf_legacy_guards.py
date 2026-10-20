"""A legacy resource stays untouched when any current ownership evidence is missing."""

import subprocess

import pytest

from app.services.cf_tunnels.legacy import LegacyTunnelRuntime, inspect_legacy
from app.services.cf_tunnels.models import CFError
from tests.test_cf_legacy import (
    make_asset,
    make_proc_root,
    make_systemd_mock,
    write_unit,
)


def legacy_fixture(tmp_path, monkeypatch):
    project = tmp_path / "business"
    project.mkdir(exist_ok=True)
    units_root = tmp_path / "units"
    unit = write_unit(
        units_root,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start="/usr/bin/cloudflared tunnel --url http://127.0.0.1:18080",
    )
    info = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "MainPID": "0",
        "InvocationID": "",
        "FragmentPath": str(unit),
        "WorkingDirectory": str(project),
        "ExecStart": "/usr/bin/cloudflared tunnel --url http://127.0.0.1:18080",
    }
    monkeypatch.setattr(subprocess, "run", make_systemd_mock({unit.name: info}))
    runtime = LegacyTunnelRuntime(make_proc_root(tmp_path))
    legacy = inspect_legacy(make_asset(project), unit_roots=[units_root])
    return project, units_root, runtime, legacy, info


@pytest.mark.parametrize(
    "field,value,stage",
    [
        ("InvocationID", "", "legacy_identity"),
        ("FragmentPath", "", "legacy_identity"),
        ("LoadState", "masked", "legacy_masked"),
    ],
)
def test_legacy_close_requires_complete_current_ownership_evidence(
    tmp_path, monkeypatch, field, value, stage
):
    _, _, runtime, legacy, info = legacy_fixture(tmp_path, monkeypatch)
    handle = runtime.start(
        "owner", "token", legacy["origin"], timeout=10, legacy=legacy
    )
    info[field] = value
    with pytest.raises(CFError) as error:
        runtime.stop(handle, timeout=15)
    assert error.value.stage == stage
    assert info["ActiveState"] == "active"


@pytest.mark.parametrize("host", ["127.business.invalid", "10.business.invalid"])
def test_legacy_origin_prefix_is_not_proof_of_a_private_ip(tmp_path, host):
    project = tmp_path / "business"
    project.mkdir()
    units = tmp_path / "units"
    write_unit(
        units,
        "legacy-cf.service",
        working_directory=str(project),
        exec_start=f"/usr/bin/cloudflared tunnel --url http://{host}:18080",
    )
    hint = inspect_legacy(make_asset(project), unit_roots=[units])
    assert hint["management"] == "external_hint"


def test_legacy_url_comes_from_the_latest_output_of_the_current_live_invocation(
    tmp_path, monkeypatch
):
    _, _, runtime, legacy, info = legacy_fixture(tmp_path, monkeypatch)
    handle = runtime.start(
        "owner", "token", legacy["origin"], timeout=10, legacy=legacy
    )
    external_run = subprocess.run

    def journal(command, **kwargs):
        if "journalctl" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                "https://older-fixture.trycloudflare.com\nhttps://current-fixture.trycloudflare.com\n",
                "",
            )
        return external_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", journal)
    assert runtime.observe(handle)["url"] == "https://current-fixture.trycloudflare.com"
    info.update(ActiveState="activating", MainPID="0")
    assert runtime.observe(handle)["active"] is False
