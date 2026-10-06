from pathlib import Path
import json
import subprocess
import sys

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.services.asset_policies import AssetPolicyService
from app.tasks.store import InMemoryTaskStore


def rules(tmp_path):
    (tmp_path / "categories").mkdir()
    (tmp_path / "objects").mkdir()
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules/python-packages.yaml").write_text("""packages:
  - name: Some_Package
    managed_by: python
    allowed_actions: [update_latest]
  - name: protected-package
    managed_by: python
    protected: true
""")
    return AssetPolicyService(tmp_path)


def test_python_rule_lookup_normalizes_distribution_name(tmp_path):
    service = rules(tmp_path)
    assert service.check_package_action("python", "some-package", "delete")[0] is False
    assert (
        service.check_package_action(
            "python", "SOME.PACKAGE", "install_or_update", version="1.0"
        )[0]
        is False
    )


def test_python_protected_flag_applies_to_all_actions(tmp_path):
    service = rules(tmp_path)
    assert (
        service.check_package_action(
            "python", "protected-package", "install_or_update"
        )[0]
        is False
    )
    assert (
        service.check_package_action("python", "protected-package", "delete")[0]
        is False
    )


def test_selected_version_requires_deploy_permission_at_http_entry(tmp_path):
    rules(tmp_path)
    client = TestClient(
        create_app(config_root=tmp_path, task_store=InMemoryTaskStore())
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())
    response = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_names": ["Some_Package"],
            "version_map": {"Some_Package": "1.0"},
            "node_ids": ["__local__"],
        },
    )
    assert response.status_code == 202
    assert response.json()["queued_count"] == 0


def test_non_whitelisted_install_checks_target_before_mutation(tmp_path):
    rules(tmp_path)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    response = client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_names": ["pytest"],
            "node_ids": ["__local__"],
        },
    )
    assert response.status_code == 202
    task = store.list_all()[0]
    commands = json.loads(Path(task.plan_path).read_text())["commands"]
    # A preflight precedes pip and runs on the exact target interpreter.
    assert len(commands) >= 2, (
        "missing install-only guard for an unmanaged installed package"
    )
    assert commands[0][0] == commands[1][0]
    checked = subprocess.run(
        [sys.executable, *commands[0][1:]], capture_output=True, text=True
    )
    assert checked.returncode != 0
    assert "白名单" in checked.stderr


def test_new_package_install_preflight_allows_absent_distribution(tmp_path):
    rules(tmp_path)
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    client.post(
        "/api/packages/install",
        json={
            "tool_type": "python",
            "package_names": ["wsl-fixture-absent-936752"],
            "node_ids": ["__local__"],
        },
    )
    commands = json.loads(Path(store.list_all()[0].plan_path).read_text())["commands"]
    assert len(commands) >= 2
    checked = subprocess.run(
        [sys.executable, *commands[0][1:]], capture_output=True, text=True
    )
    assert checked.returncode == 0


def test_custom_python_guard_uses_selected_interpreter(tmp_path):
    import shlex
    from app.api.remote_nodes import _guard_package_shell

    policy = rules(tmp_path)
    command = _guard_package_shell(policy, 'python', 'install_or_update',
                                   ['absent-fixture'], 'python -m pip install absent-fixture', pip_only=True)
    assert shlex.split(command)[0] == 'python'


def test_custom_pip_guard_checks_pip_environment_not_python3(tmp_path):
    import os
    from app.api.remote_nodes import _guard_package_shell

    policy = rules(tmp_path)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    marker = tmp_path / 'installed'
    pip = bindir / 'pip'
    pip.write_text('#!/bin/sh\nif [ "$1" = list ]; then printf \'[{"name":"other-env-only","version":"1"}]\'; else touch "' + str(marker) + '"; fi\n')
    pip.chmod(0o755)
    command = _guard_package_shell(policy, 'python', 'install_or_update',
                                   ['other-env-only'], 'pip install other-env-only', pip_only=True)
    result = subprocess.run(['sh', '-c', command], env={**os.environ, 'PATH': str(bindir) + ':' + os.environ['PATH']}, capture_output=True)
    assert result.returncode != 0
    assert not marker.exists()
