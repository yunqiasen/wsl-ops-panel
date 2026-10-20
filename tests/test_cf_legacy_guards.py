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
