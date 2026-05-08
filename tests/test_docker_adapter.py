import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.docker_adapter import DockerComposeAdapter
from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.models.assets import PackageVersionInfo
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


def test_deploy_version_for_compose_pull_uses_safe_generated_override_file() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/tmp/app',
        compose_file='compose.custom.yml',
        primary_container='demo-container',
        compose_service='demo-service',
        image_repository='example/demo',
        lifecycle_strategy='compose_pull',
    )

    plan = adapter.plan_action('deploy_version', version='v1.2.3')

    assert plan.commands[0][:2] == ['python3', '-c']
    assert plan.commands[1] == [
        'docker',
        'compose',
        '-f',
        'compose.custom.yml',
        '-f',
        '.wsl-ops-panel.override.yml',
        'up',
        '-d',
        '--no-build',
        'demo-service',
    ]
    assert plan.commands[2] == ['rm', '-f', '.wsl-ops-panel.override.yml']


def test_openai_cpa_update_latest_builds_local_image_from_git_tag() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/openai-cpa',
        override_file='/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
        local_image_repository='local/wenfxl-codex-manager',
        local_image_tag_template='{version}-overlay',
        healthcheck_url='http://127.0.0.1:8128',
    )

    plan = adapter.plan_action('deploy_version', version='v14.2.6')

    assert plan.commands[:4] == [
        ['git', '-C', '/srv/openai-cpa', 'fetch', '--tags', '--force', 'origin'],
        ['git', '-C', '/srv/openai-cpa', 'checkout', 'v14.2.6'],
        ['docker', 'build', '-t', 'local/wenfxl-codex-manager:v14.2.6-overlay', '-f', 'Dockerfile', '.'],
        [
            'env',
            'WSL_OPS_IMAGE=local/wenfxl-codex-manager:v14.2.6-overlay',
            'docker',
            'compose',
            '-f',
            'docker-compose.yml',
            '-f',
            '/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
            'up',
            '-d',
            '--no-build',
            'codex-web',
        ],
    ]


def test_get_version_info_uses_strategy_specific_version_source(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        image_repository='example/demo',
        current_version='v14.2.6',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/openai-cpa',
    )

    def fake_git_tags(repo_dir: str) -> PackageVersionInfo:
        assert repo_dir == '/srv/openai-cpa'
        return PackageVersionInfo(current_version='v14.2.6', latest_version='v14.2.7', versions=['v14.2.7', 'v14.2.6'])

    def fail_registry(*args, **kwargs) -> PackageVersionInfo:
        raise AssertionError('registry lookup should not be used')

    monkeypatch.setattr(adapter._version_service, 'get_git_tag_version_info', fake_git_tags)
    monkeypatch.setattr(adapter._version_service, 'get_registry_tag_version_info', fail_registry)

    info = adapter.get_version_info()

    assert info.current_version == 'v14.2.6'
    assert info.latest_version == 'v14.2.7'
    assert info.lifecycle_strategy == 'compose_local_build_git_tag'
    assert info.version_source == 'git_tags'


def test_asset_action_endpoints_enqueue_tasks_and_persist_plan(tmp_path: Path, monkeypatch) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml(compose_file='compose.custom.yml'))

    from app.scanners.docker_scanner import parse_docker_ps_lines

    monkeypatch.setattr(
        DockerComposeAdapter,
        'get_version_info',
        lambda self: PackageVersionInfo(current_version='latest', latest_version='latest', versions=['latest']),
    )

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
