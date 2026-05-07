from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.models.assets import PackageVersionInfo
from app.scanners.node_scanner import parse_npm_package
from app.scanners.python_scanner import parse_pip_package
from app.tasks.store import InMemoryTaskStore


def test_node_update_latest_route_enqueues_task_for_actionable_package(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')

    asset = parse_npm_package('update@0.7.4')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(f'/api/assets/{asset.object_id}/actions/update-latest')

    assert response.status_code == 202
    payload = response.json()
    assert payload['plan']['commands'] == [['npm', 'install', '-g', 'update@latest']]


def test_python_versions_route_returns_source_status(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'python.yaml').write_text('id: python\nlabel: Python\norder: 40\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text(
        'packages:\n  - name: fastapi\n    managed_by: python\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )

    asset = parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})
    client = TestClient(create_app(config_root=tmp_path, python_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    from app.api import assets as assets_api

    monkeypatch.setattr(
        assets_api.PythonPackageAdapter,
        'get_version_info',
        lambda self: PackageVersionInfo(
            current_version='0.115.0',
            latest_version='0.116.0',
            versions=['0.115.0', '0.116.0'],
            source_status='ok',
        ),
    )

    response = client.get(f'/api/assets/{asset.object_id}/versions')

    assert response.status_code == 200
    payload = response.json()
    assert payload['latest_version'] == '0.116.0'
    assert payload['source_status'] == 'ok'


def test_protected_node_package_returns_conflict_on_action(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: "@openai/codex"\n    managed_by: agent_cli\n    protected: true\n    blocked_reason: 保留给 agent cli\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    asset = parse_npm_package('@openai/codex@0.128.0')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(f'/api/assets/{asset.object_id}/actions/delete')

    assert response.status_code == 409
    assert response.json()['detail'] == '保留给 agent cli'


def test_non_whitelisted_python_package_detail_is_read_only(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'python.yaml').write_text('id: python\nlabel: Python\norder: 40\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    asset = parse_pip_package({'name': 'requests', 'version': '2.32.0'})
    client = TestClient(create_app(config_root=tmp_path, python_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get(f'/assets/{asset.object_id}')

    assert response.status_code == 200
    assert '白名单外' in response.text
    assert 'hx-post="/api/assets/' not in response.text
