import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.docker_adapter import DockerComposeAdapter
from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.tasks.store import InMemoryTaskStore


def _write_registry_file(root: Path, folder: str, name: str, content: str) -> None:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding='utf-8')


def _docker_object_yaml(
    *,
    object_id: str = 'cpa',
    compose_file: str = 'docker-compose.yml',
    project_dir: str = '/srv/cpa',
) -> str:
    return (
        f'id: {object_id}\n'
        'category: docker\n'
        'type: docker_compose\n'
        'name: CPA / CLIProxyAPI\n'
        'config:\n'
        f'  project_dir: {project_dir}\n'
        f'  compose_file: {compose_file}\n'
        '  primary_container: cli-proxy-api\n'
        '  compose_service: cli-proxy-api\n'
    )


def _docker_ps_line(*, service: str = 'cli-proxy-api') -> str:
    return json.dumps(
        {
            'ID': 'abc123',
            'Image': 'eceasy/cli-proxy-api:latest',
            'Labels': (
                'com.docker.compose.project=cliproxyapi,'
                'com.docker.compose.project.working_dir=/srv/cpa,'
                f'com.docker.compose.service={service}'
            ),
            'Names': 'cli-proxy-api',
            'State': 'running',
            'Status': 'Up 3 days',
            'Ports': '8317/tcp',
        }
    )


def _login_test_client(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, issue_session_token())


def test_update_latest_uses_pull_and_up_with_explicit_compose_file() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/tmp/app',
        compose_file='compose.custom.yml',
        primary_container='demo-container',
        compose_service='demo-service',
    )

    plan = adapter.plan_action('update_latest')

    assert plan.commands == [
        ['docker', 'compose', '-f', 'compose.custom.yml', 'pull', 'demo-service'],
        ['docker', 'compose', '-f', 'compose.custom.yml', 'up', '-d', '--no-build', 'demo-service'],
    ]
    assert plan.working_dir == '/tmp/app'


def test_full_delete_preview_includes_paths_and_objects() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/tmp/app',
        compose_file='docker-compose.yml',
        primary_container='demo-container',
        compose_service='demo-service',
        image_repository='example/demo',
    )

    plan = adapter.plan_action('full_delete')

    assert plan.preview_paths == ['/tmp/app', '/tmp/app/docker-compose.yml']
    assert plan.preview_objects == ['demo-service', 'demo-container', 'example/demo']
    assert plan.commands[0] == [
        'docker',
        'compose',
        '-f',
        'docker-compose.yml',
        'down',
        '--remove-orphans',
        '--rmi',
        'all',
        '--volumes',
    ]
    assert plan.commands[-1] == ['rm', '-rf', '/tmp/app']


def test_asset_action_endpoints_enqueue_tasks_and_persist_plan(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml(compose_file='compose.custom.yml'))

    from app.scanners.docker_scanner import parse_docker_ps_lines

    containers = parse_docker_ps_lines([_docker_ps_line()])
    store = InMemoryTaskStore()
    client = TestClient(
        create_app(
            config_root=tmp_path,
            docker_scanner=lambda: containers,
            task_store=store,
        )
    )
    _login_test_client(client)

    versions_response = client.get('/api/assets/cpa/versions')
    assert versions_response.status_code == 200
    assert versions_response.json()['versions'] == ['latest']

    preview_response = client.get('/api/assets/cpa/actions/full-delete-preview')
    assert preview_response.status_code == 200
    assert preview_response.json()['preview_paths'] == ['/srv/cpa', '/srv/cpa/compose.custom.yml']

    update_response = client.post('/api/assets/cpa/actions/update-latest')
    assert update_response.status_code == 202
    update_payload = update_response.json()
    assert update_payload['task']['object_id'] == 'cpa'
    assert update_payload['task']['action'] == 'update_latest'
    assert update_payload['plan']['commands'] == [
        ['docker', 'compose', '-f', 'compose.custom.yml', 'pull', 'cli-proxy-api'],
        ['docker', 'compose', '-f', 'compose.custom.yml', 'up', '-d', '--no-build', 'cli-proxy-api'],
    ]

    deploy_response = client.post('/api/assets/cpa/actions/deploy-version', json={'version': 'v1.2.3'})
    assert deploy_response.status_code == 202
    deploy_payload = deploy_response.json()
    assert deploy_payload['task']['requested_version'] == 'v1.2.3'
    assert deploy_payload['plan']['preview_objects'] == ['eceasy/cli-proxy-api:v1.2.3']

    delete_response = client.post('/api/assets/cpa/actions/delete')
    assert delete_response.status_code == 202
    assert delete_response.json()['plan']['commands'] == [
        ['docker', 'compose', '-f', 'compose.custom.yml', 'rm', '-f', '-s', 'cli-proxy-api']
    ]

    full_delete_response = client.post('/api/assets/cpa/actions/full-delete')
    assert full_delete_response.status_code == 202
    assert full_delete_response.json()['task']['action'] == 'full_delete'

    queued = store.list_all()
    assert [task.action for task in queued] == ['update_latest', 'deploy_version', 'delete', 'full_delete']
    assert queued[0].stdout_log_path.endswith('/stdout.log')
    assert queued[0].stderr_log_path.endswith('/stderr.log')
    assert queued[0].plan_path is not None
    assert Path(queued[0].plan_path).exists()


def test_deploy_version_works_for_stopped_asset_using_compose_image_metadata(tmp_path: Path) -> None:
    project_dir = tmp_path / 'project'
    project_dir.mkdir()
    (project_dir / 'compose.custom.yml').write_text(
        'services:\n  cli-proxy-api:\n    image: ${CLI_PROXY_IMAGE:-eceasy/cli-proxy-api:latest}\n',
        encoding='utf-8',
    )
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'cpa.yaml',
        _docker_object_yaml(compose_file='compose.custom.yml', project_dir=str(project_dir)),
    )

    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: [], task_store=store))
    _login_test_client(client)

    response = client.post('/api/assets/cpa/actions/deploy-version', json={'version': 'v9.9.9'})

    assert response.status_code == 202
    payload = response.json()
    assert payload['plan']['preview_objects'] == ['eceasy/cli-proxy-api:v9.9.9']
    assert payload['plan']['commands'][1][:6] == [
        'docker',
        'compose',
        '-f',
        'compose.custom.yml',
        '-f',
        '.wsl-ops-panel.override.yml',
    ]
