from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.scanners.systemd_scanner import parse_systemctl_line
from app.tasks.store import InMemoryTaskStore


def _write_registry_file(root: Path, folder: str, name: str, content: str) -> None:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding='utf-8')


def _systemd_object_yaml(*, object_id: str = 'wsl_ops_panel', unit_name: str = 'wsl-ops-panel.service') -> str:
    return (
        f'id: {object_id}\n'
        'category: systemd\n'
        'type: systemd_unit\n'
        'name: WSL Ops Panel\n'
        'config:\n'
        f'  unit_name: {unit_name}\n'
        '  working_dir: /srv/wsl-ops-panel\n'
    )


def _raise_systemctl_missing():
    raise FileNotFoundError('systemctl')


def test_parse_systemctl_line() -> None:
    asset = parse_systemctl_line('cftunnel.service loaded active running cftunnel for CLIProxyAPI')

    assert asset.name == 'cftunnel.service'
    assert asset.status == 'active'
    assert asset.metadata == {
        'load_state': 'loaded',
        'sub': 'running',
        'description': 'cftunnel for CLIProxyAPI',
    }


def test_systemd_category_detail_and_delete_routes(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'categories', 'systemd.yaml', 'id: systemd\nlabel: systemd\norder: 20\n')
    _write_registry_file(tmp_path, 'objects', 'panel.yaml', _systemd_object_yaml())

    scanned_assets = [parse_systemctl_line('wsl-ops-panel.service loaded active running WSL Ops Panel service')]
    client = TestClient(
        create_app(
            config_root=tmp_path,
            systemd_scanner=lambda: scanned_assets,
            task_store=InMemoryTaskStore(),
        )
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    category_response = client.get('/categories/systemd')
    assert category_response.status_code == 200
    assert 'WSL Ops Panel' in category_response.text
    assert 'wsl-ops-panel.service' in category_response.text

    detail_response = client.get('/assets/wsl_ops_panel')
    assert detail_response.status_code == 200
    assert 'WSL Ops Panel' in detail_response.text
    assert '/srv/wsl-ops-panel' in detail_response.text
    assert 'WSL Ops Panel service' in detail_response.text

    versions_response = client.get('/api/assets/wsl_ops_panel/versions')
    assert versions_response.status_code == 200
    assert versions_response.json()['versions'] == []

    delete_response = client.post('/api/assets/wsl_ops_panel/actions/delete')
    assert delete_response.status_code == 202
    assert delete_response.json()['plan']['commands'] == [
        ['sudo', 'systemctl', 'disable', '--now', 'wsl-ops-panel.service']
    ]


def test_systemd_scan_failure_surfaces_explicit_status(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'categories', 'systemd.yaml', 'id: systemd\nlabel: systemd\norder: 20\n')
    _write_registry_file(tmp_path, 'objects', 'panel.yaml', _systemd_object_yaml())

    client = TestClient(
        create_app(
            config_root=tmp_path,
            systemd_scanner=_raise_systemctl_missing,
            task_store=InMemoryTaskStore(),
        )
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/assets/wsl_ops_panel')

    assert response.status_code == 200
    assert 'scan_failed' in response.text
