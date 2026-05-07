from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.scanners.node_scanner import parse_npm_package


def test_node_update_latest_route_enqueues_task_for_actionable_package(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')

    asset = parse_npm_package('update@0.7.4')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset]))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(f'/api/assets/{asset.object_id}/actions/update-latest')

    assert response.status_code == 202
    payload = response.json()
    assert payload['plan']['commands'] == [['npm', 'install', '-g', 'update@latest']]
