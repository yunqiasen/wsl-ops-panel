from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.scanners.docker_scanner import parse_docker_ps_lines
from app.tasks.store import InMemoryTaskStore
from app.models.assets import AssetSnapshot


def _write_registry(root: Path) -> None:
    (root / 'categories').mkdir(parents=True, exist_ok=True)
    (root / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (root / 'objects').mkdir(parents=True, exist_ok=True)
    (root / 'objects' / 'cpa.yaml').write_text(
        'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\nconfig:\n'
        '  project_dir: /srv/cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: cli-proxy-api\n  compose_service: cli-proxy-api\n',
        encoding='utf-8',
    )
    (root / 'objects' / 'new-api.yaml').write_text(
        'id: new_api\ncategory: docker\ntype: docker_compose\nname: New API\nconfig:\n'
        '  project_dir: /srv/new-api\n  compose_file: docker-compose.yml\n'
        '  primary_container: new-api\n  compose_service: new-api\n',
        encoding='utf-8',
    )


def _containers():
    return parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"eceasy/cli-proxy-api:latest","Labels":"com.docker.compose.project=cpa,com.docker.compose.project.working_dir=/srv/cpa,com.docker.compose.service=cli-proxy-api","Names":"cli-proxy-api","State":"running","Status":"Up 1 hour","Ports":"8317/tcp"}',
            '{"ID":"2","Image":"calciumion/new-api:latest","Labels":"com.docker.compose.project=new-api,com.docker.compose.project.working_dir=/srv/new-api,com.docker.compose.service=new-api","Names":"new-api","State":"running","Status":"Up 1 hour","Ports":"38217/tcp"}',
        ]
    )


def _client(tmp_path: Path, store: InMemoryTaskStore) -> TestClient:
    _write_registry(tmp_path)
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=_containers, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    return client


def test_bulk_update_latest_enqueues_one_task_per_asset(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    client = _client(tmp_path, store)

    response = client.post('/api/bulk/actions/update-latest', json={'asset_ids': ['cpa', 'new_api']})

    assert response.status_code == 202
    payload = response.json()
    assert payload['queued_count'] == 2
    assert [task['object_id'] for task in payload['tasks']] == ['cpa', 'new_api']
    assert [task.action for task in store.list_all()] == ['update_latest', 'update_latest']


def test_bulk_deploy_version_uses_per_asset_version_map(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    client = _client(tmp_path, store)

    response = client.post(
        '/api/bulk/actions/deploy-version',
        json={'asset_ids': ['cpa', 'new_api'], 'version_map': {'cpa': 'v1', 'new_api': 'v2'}},
    )

    assert response.status_code == 202
    tasks = store.list_all()
    assert [(task.object_id, task.action, task.requested_version) for task in tasks] == [
        ('cpa', 'deploy_version', 'v1'),
        ('new_api', 'deploy_version', 'v2'),
    ]


def test_bulk_start_enqueues_docker_start_plan(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    client = _client(tmp_path, store)

    response = client.post('/api/bulk/actions/start', json={'asset_ids': ['cpa']})

    assert response.status_code == 202
    task = store.list_all()[0]
    assert task.action == 'start'
    assert task.plan_path is not None
    assert 'docker' in Path(task.plan_path).read_text(encoding='utf-8')


def test_bulk_cf_action_skips_node_asset_even_if_endpoint_exists(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: update\n    managed_by: node\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    from app.scanners.node_scanner import parse_npm_package

    asset = parse_npm_package('update@0.7.4')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post('/api/bulk/actions/cf-refresh', json={'asset_ids': [asset.object_id]})

    assert response.status_code == 202
    payload = response.json()
    assert payload['queued_count'] == 0
    assert payload['tasks'] == []
    assert payload['skipped'] == [
        {
            'asset_id': asset.object_id,
            'reason': f'action cf_refresh is not supported by {asset.object_id}',
        }
    ]


def test_bulk_cf_action_continues_after_skipping_asset_without_cf_control(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    cpa_dir = tmp_path / 'cpa'
    cpa_dir.mkdir()
    (cpa_dir / 'scripts').mkdir()
    (cpa_dir / 'scripts' / 'cftunnel-start.sh').write_text('#!/usr/bin/env bash\n', encoding='utf-8')
    no_cf_dir = tmp_path / 'no-cf'
    no_cf_dir.mkdir()
    (tmp_path / 'objects' / 'cpa.yaml').write_text(
        f'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\nconfig:\n'
        f'  project_dir: {cpa_dir}\n  compose_file: docker-compose.yml\n'
        '  primary_container: cli-proxy-api\n  compose_service: cli-proxy-api\n',
        encoding='utf-8',
    )
    (tmp_path / 'objects' / 'no-cf.yaml').write_text(
        f'id: no_cf\ncategory: docker\ntype: docker_compose\nname: No CF\nconfig:\n'
        f'  project_dir: {no_cf_dir}\n  compose_file: docker-compose.yml\n'
        '  primary_container: no-cf\n  compose_service: no-cf\n',
        encoding='utf-8',
    )
    containers = parse_docker_ps_lines(
        [
            f'{{"ID":"1","Image":"eceasy/cli-proxy-api:latest","Labels":"com.docker.compose.project=cpa,com.docker.compose.project.working_dir={cpa_dir},com.docker.compose.service=cli-proxy-api","Names":"cli-proxy-api","State":"running","Status":"Up 1 hour","Ports":"8317/tcp"}}',
            f'{{"ID":"2","Image":"demo/no-cf:latest","Labels":"com.docker.compose.project=no-cf,com.docker.compose.project.working_dir={no_cf_dir},com.docker.compose.service=no-cf","Names":"no-cf","State":"running","Status":"Up 1 hour","Ports":"8080/tcp"}}',
        ]
    )
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post('/api/bulk/actions/cf-refresh', json={'asset_ids': ['no_cf', 'cpa']})

    assert response.status_code == 202
    payload = response.json()
    assert payload['queued_count'] == 1
    assert [task['object_id'] for task in payload['tasks']] == ['cpa']
    assert payload['skipped'] == [{'asset_id': 'no_cf', 'reason': 'action cf_refresh is not supported by no_cf'}]
    assert [task.object_id for task in store.list_all()] == ['cpa']


def test_asset_level_start_endpoint_enqueues_host_port_plan(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'host.yaml').write_text('id: host\nlabel: 宿主机进程\norder: 80\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    from app.scanners.host_process_scanner import parse_listening_socket

    host_asset = parse_listening_socket('LISTEN 0 128 0.0.0.0:8317 0.0.0.0:* users:(("docker-proxy",pid=1234,fd=7))')
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, host_process_scanner=lambda: [host_asset], task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(f'/api/assets/{host_asset.object_id}/actions/stop')

    assert response.status_code == 202
    payload = response.json()
    assert payload['task']['action'] == 'stop'
    assert payload['plan']['commands'] == [['docker', 'stop', 'cli-proxy-api']]
    assert 'cli-proxy-api' in payload['plan']['preview_objects']


def test_host_port_endpoint_uses_container_name_for_runtime_discovered_port(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'host.yaml').write_text('id: host\nlabel: 宿主机进程\norder: 80\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    from app.scanners.host_process_scanner import parse_listening_socket

    host_asset = parse_listening_socket('LISTEN 0 128 0.0.0.0:8312 0.0.0.0:* users:(("docker-proxy",pid=1234,fd=7))')
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, host_process_scanner=lambda: [host_asset], task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post(f'/api/assets/{host_asset.object_id}/actions/start')

    assert response.status_code == 202
    payload = response.json()
    assert payload['plan']['commands'] == [['docker', 'start', 'image2api']]


def test_bulk_project_update_uses_project_adapter(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'project.yaml').write_text('id: project\nlabel: Project\norder: 11\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    asset = AssetSnapshot(
        object_id='project__wsl-ops-panel',
        category='project',
        name='wsl-ops-panel',
        status='present',
        current_version='main',
        metadata={'path': '/srv/wsl-ops-panel', 'stacks': ['python'], 'git_remote_url': 'https://github.com/example/wsl-ops-panel'},
    )
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, project_scanner=lambda: [asset], task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post('/api/bulk/actions/update-latest', json={'asset_ids': [asset.object_id]})

    assert response.status_code == 202
    payload = response.json()
    assert payload['queued_count'] == 1
    assert payload['skipped'] == []
    assert payload['tasks'][0]['object_id'] == asset.object_id
