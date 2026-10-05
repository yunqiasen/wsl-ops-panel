import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.adapters.docker_adapter import DockerComposeAdapter
from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.models.assets import PackageVersionInfo, RuntimeVersionInfo
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
        ['docker', 'compose', '-f', 'compose.custom.yml', 'pull', '--ignore-buildable', 'demo-service'],
        ['docker', 'compose', '-f', 'compose.custom.yml', 'up', '-d', '--no-build', 'demo-service'],
    ]
    assert plan.retry_policy == {
        'max_attempts': 3,
        'delay_seconds': 5.0,
        'retry_on_stderr': [
            'EOF',
            'TLS handshake timeout',
            'connection reset by peer',
            'gnutls_handshake() failed',
            'The TLS connection was non-properly terminated',
            'ConnectionResetError',
        ],
    }
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

    assert len(plan.commands) == 1
    assert plan.commands[0][:2] == ['python3', '-c']
    assert 'finally:' in plan.commands[0][2]
    assert plan.commands[0][3:] == ['.wsl-ops-panel.override.yml', 'compose.custom.yml', 'demo-service', 'example/demo:v1.2.3']
    assert 'subprocess.run(["docker", "compose", "-f", compose_file, "-f", str(override), "up", "-d", "--no-build", service], check=True)' in plan.commands[0][2]


def test_openai_cpa_update_latest_builds_local_image_from_latest_git_tag(monkeypatch) -> None:
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

    def fake_git_tags(repo_dir: str, *, fetch: bool = False, remote: bool = False) -> PackageVersionInfo:
        assert repo_dir == '/srv/openai-cpa'
        assert fetch is False
        return PackageVersionInfo(latest_version='v14.2.6', versions=['v14.2.6', 'v14.2.5'])

    monkeypatch.setattr(adapter._version_service, 'get_git_tag_version_info', fake_git_tags)

    plan = adapter.plan_action('update_latest')

    assert plan.commands[:3] == [
        ['git', '-C', '/srv/openai-cpa', 'checkout', 'v14.2.6'],
        [
            'docker',
            'build',
            '-t',
            'local/wenfxl-codex-manager:v14.2.6-overlay',
            '--label',
            'org.opencontainers.image.version=14.2.6',
            '--label',
            'org.opencontainers.image.source=https://github.com/wenfxl/openai-cpa',
            '-f',
            'Dockerfile',
            '.',
        ],
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
    assert plan.working_dir == '/srv/openai-cpa'
    assert plan.preview_objects == ['local/wenfxl-codex-manager:v14.2.6-overlay', 'v14.2.6']


def test_openai_cpa_update_latest_can_build_from_clean_worktree(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        current_version='v15.0.2',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/openai-cpa',
        override_file='/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
        local_image_repository='local/wenfxl-codex-manager',
        local_image_tag_template='{version}-overlay',
        build_worktree_dir='/panel/data/build-worktrees/openai-cpa',
        healthcheck_url='http://127.0.0.1:8128',
        runtime=RuntimeVersionInfo(oci_version='15.0.2', image_tag='latest'),
    )

    def fake_git_tags(repo_dir: str, *, fetch: bool = False, remote: bool = False) -> PackageVersionInfo:
        assert repo_dir == '/srv/openai-cpa'
        assert remote is True
        return PackageVersionInfo(latest_version='v15.0.3', versions=['v15.0.3', 'v15.0.2'])

    monkeypatch.setattr(adapter._version_service, 'get_git_tag_version_info', fake_git_tags)

    plan = adapter.plan_action('update_latest')

    assert plan.commands[0] == ['rm', '-rf', '/panel/data/build-worktrees/openai-cpa/v15.0.3']
    assert plan.commands[1][0:3] == ['python3', '-c', plan.commands[1][2]]
    assert plan.commands[1][3:] == [
        '/srv/openai-cpa',
        'v15.0.3',
        'refs/tags/wsl-ops-panel/remote-tags/v15.0.3',
    ]
    assert plan.commands[2] == [
        'git',
        '-C',
        '/srv/openai-cpa',
        'worktree',
        'add',
        '--force',
        '--detach',
        '/panel/data/build-worktrees/openai-cpa/v15.0.3',
        'refs/tags/wsl-ops-panel/remote-tags/v15.0.3',
    ]
    assert plan.commands[3] == [
        'docker',
        'build',
        '-t',
        'local/wenfxl-codex-manager:v15.0.3-overlay',
        '--label',
        'org.opencontainers.image.version=15.0.3',
        '--label',
        'org.opencontainers.image.source=https://github.com/wenfxl/openai-cpa',
        '-f',
        '/panel/data/build-worktrees/openai-cpa/v15.0.3/Dockerfile',
        '/panel/data/build-worktrees/openai-cpa/v15.0.3',
    ]
    assert plan.commands[4][0:8] == [
        'env',
        'WSL_OPS_IMAGE=local/wenfxl-codex-manager:v15.0.3-overlay',
        'docker',
        'compose',
        '-f',
        '/srv/openai-cpa/docker-compose.yml',
        '-f',
        '/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
    ]
    assert plan.commands[5] == [
        'python3',
        '-c',
        plan.commands[-1][2],
        'http://127.0.0.1:8128',
    ]
    assert plan.working_dir == '/srv/openai-cpa'
    assert plan.preview_objects == ['local/wenfxl-codex-manager:v15.0.3-overlay', 'v15.0.3']


def test_worktree_build_injects_pip_build_args_into_derived_dockerfile() -> None:
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
        build_worktree_dir='/panel/data/build-worktrees/openai-cpa',
        docker_build_args={
            'PIP_INDEX_URL': 'https://pypi.tuna.tsinghua.edu.cn/simple',
            'PIP_DEFAULT_TIMEOUT': '120',
        },
    )

    plan = adapter.plan_action('deploy_version', version='v15.0.3')

    assert plan.commands[3][:2] == ['python3', '-c']
    assert 'PIP_INDEX_URL' in plan.commands[3]
    assert 'PIP_DEFAULT_TIMEOUT' in plan.commands[3]
    assert plan.commands[4] == [
        'docker',
        'build',
        '-t',
        'local/wenfxl-codex-manager:v15.0.3-overlay',
        '--label',
        'org.opencontainers.image.version=15.0.3',
        '--label',
        'org.opencontainers.image.source=https://github.com/wenfxl/openai-cpa',
        '--build-arg',
        'PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple',
        '--build-arg',
        'PIP_DEFAULT_TIMEOUT=120',
        '-f',
        '/panel/data/build-worktrees/openai-cpa/v15.0.3/Dockerfile',
        '/panel/data/build-worktrees/openai-cpa/v15.0.3',
    ]
    assert plan.commands[5][0:8] == [
        'env',
        'WSL_OPS_IMAGE=local/wenfxl-codex-manager:v15.0.3-overlay',
        'docker',
        'compose',
        '-f',
        '/srv/openai-cpa/docker-compose.yml',
        '-f',
        '/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
    ]


def test_docker_build_args_can_expand_proxy_gateway() -> None:
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
        docker_build_args={
            'HTTP_PROXY': '{docker_bridge_proxy_url}',
            'HTTPS_PROXY': '{docker_bridge_proxy_url}',
            'PIP_INDEX_URL': 'https://pypi.tuna.tsinghua.edu.cn/simple',
        },
    )

    plan = adapter.plan_action('deploy_version', version='v15.0.3')

    assert '--build-arg' in plan.commands[1]
    assert 'HTTP_PROXY=http://172.17.0.1:7890' in plan.commands[1]
    assert 'HTTPS_PROXY=http://172.17.0.1:7890' in plan.commands[1]
    assert 'PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple' in plan.commands[1]



def test_sub2api_update_latest_can_build_local_git_tag_without_registry_pull(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/sub2api',
        compose_file='docker-compose.yml',
        primary_container='sub2api',
        compose_service='sub2api',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/sub2api',
        override_file='/panel/config/recipes/docker/overrides/sub2api.compose.override.yaml',
        local_image_repository='local/sub2api',
        local_image_tag_template='{version}-wsl',
        docker_build_args={
            'VERSION': '{version_without_v}',
            'COMMIT': '{version}',
            'HTTP_PROXY': 'http://172.17.0.1:7890',
        },
        healthcheck_url='http://127.0.0.1:8420/health',
    )

    def fake_git_tags(repo_dir: str, *, fetch: bool = False, remote: bool = False) -> PackageVersionInfo:
        assert repo_dir == '/srv/sub2api'
        assert fetch is False
        return PackageVersionInfo(latest_version='v0.1.129', versions=['v0.1.129', 'v0.1.128'])

    monkeypatch.setattr(adapter._version_service, 'get_git_tag_version_info', fake_git_tags)

    plan = adapter.plan_action('update_latest')

    assert plan.commands[:3] == [
        ['git', '-C', '/srv/sub2api', 'checkout', 'v0.1.129'],
        [
            'docker',
            'build',
            '-t',
            'local/sub2api:v0.1.129-wsl',
            '--label',
            'org.opencontainers.image.version=0.1.129',
            '--build-arg',
            'VERSION=0.1.129',
            '--build-arg',
            'COMMIT=v0.1.129',
            '--build-arg',
            'HTTP_PROXY=http://172.17.0.1:7890',
            '-f',
            'Dockerfile',
            '.',
        ],
        [
            'env',
            'WSL_OPS_IMAGE=local/sub2api:v0.1.129-wsl',
            'docker',
            'compose',
            '-f',
            'docker-compose.yml',
            '-f',
            '/panel/config/recipes/docker/overrides/sub2api.compose.override.yaml',
            'up',
            '-d',
            '--no-build',
            'sub2api',
        ],
    ]
    assert plan.commands[-1] == ['python3', '-c', plan.commands[-1][2], 'http://127.0.0.1:8420/health']
    assert plan.preview_objects == ['local/sub2api:v0.1.129-wsl', 'v0.1.129']

def test_openai_cpa_deploy_version_builds_from_recipe_repo_dir_when_project_dir_differs() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/runtime-openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/source-openai-cpa',
        override_file='/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
        local_image_repository='local/wenfxl-codex-manager',
        local_image_tag_template='{version}-overlay',
    )

    plan = adapter.plan_action('deploy_version', version='v14.2.6')

    assert plan.working_dir == '/srv/source-openai-cpa'
    assert plan.commands[0] == ['git', '-C', '/srv/source-openai-cpa', 'checkout', 'v14.2.6']
    assert plan.commands[1] == [
        'docker',
        'build',
        '-t',
        'local/wenfxl-codex-manager:v14.2.6-overlay',
        '--label',
        'org.opencontainers.image.version=14.2.6',
        '-f',
        'Dockerfile',
        '.',
    ]




def test_local_git_tag_update_skips_network_fetch_when_runtime_already_latest(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/sub2api',
        compose_file='docker-compose.yml',
        primary_container='sub2api',
        compose_service='sub2api',
        current_version='v0.1.129-wsl',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/sub2api',
        override_file='/panel/config/recipes/docker/overrides/sub2api.compose.override.yaml',
        local_image_repository='local/sub2api',
        local_image_tag_template='{version}-wsl',
        healthcheck_url='http://127.0.0.1:8420/health',
    )

    monkeypatch.setattr(
        adapter._version_service,
        'get_git_tag_version_info',
        lambda repo_dir, **kwargs: PackageVersionInfo(latest_version='v0.1.129', versions=['v0.1.129', 'v0.1.128']),
    )

    plan = adapter.plan_action('update_latest')

    assert plan.commands == [
        ['python3', '-c', plan.commands[-1][2], 'http://127.0.0.1:8420/health']
    ]
    assert plan.preview_objects == ['already latest', 'v0.1.129']


def test_local_git_tag_update_skips_checkout_when_runtime_oci_version_is_latest(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        current_version='latest',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/openai-cpa',
        override_file='/panel/config/recipes/docker/overrides/openai-cpa.compose.override.yaml',
        local_image_repository='local/wenfxl-codex-manager',
        local_image_tag_template='{version}-overlay',
        healthcheck_url='http://127.0.0.1:8128',
        runtime=RuntimeVersionInfo(
            image='local/openai-cpa-patched:latest',
            image_tag='latest',
            oci_version='15.0.2',
            oci_revision='699df3af90380729456fe4dc402f39a07ce1e0ac',
        ),
    )

    monkeypatch.setattr(
        adapter._version_service,
        'get_git_tag_version_info',
        lambda repo_dir, **kwargs: PackageVersionInfo(latest_version='v15.0.2', versions=['v15.0.2', 'v15.0.1']),
    )

    plan = adapter.plan_action('update_latest')

    assert plan.commands == [
        ['python3', '-c', plan.commands[-1][2], 'http://127.0.0.1:8128']
    ]
    assert plan.preview_objects == ['already latest', 'v15.0.2']


def test_local_git_tag_update_retries_transient_git_tls_failure(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/sub2api',
        compose_file='docker-compose.yml',
        primary_container='sub2api',
        compose_service='sub2api',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/sub2api',
        override_file='/panel/config/recipes/docker/overrides/sub2api.compose.override.yaml',
        local_image_repository='local/sub2api',
        local_image_tag_template='{version}-wsl',
    )

    monkeypatch.setattr(
        adapter._version_service,
        'get_git_tag_version_info',
        lambda repo_dir, **kwargs: PackageVersionInfo(latest_version='v0.1.129', versions=['v0.1.129']),
    )

    plan = adapter.plan_action('update_latest')

    assert plan.retry_policy == {
        'max_attempts': 3,
        'delay_seconds': 5.0,
        'retry_on_stderr': [
            'EOF',
            'TLS handshake timeout',
            'connection reset by peer',
            'gnutls_handshake() failed',
            'The TLS connection was non-properly terminated',
            'ConnectionResetError',
        ],
    }

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

    def fake_git_tags(repo_dir: str, **kwargs) -> PackageVersionInfo:
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


def test_get_version_info_prefers_runtime_oci_version_when_git_head_is_not_tagged(monkeypatch) -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/openai-cpa',
        compose_file='docker-compose.yml',
        primary_container='wenfxl_codex_manager',
        compose_service='codex-web',
        current_version='latest',
        lifecycle_strategy='compose_local_build_git_tag',
        recipe_repo_dir='/srv/openai-cpa',
        runtime=RuntimeVersionInfo(
            image='local/openai-cpa-patched:latest',
            image_tag='latest',
            oci_version='15.0.2',
            oci_revision='699df3af90380729456fe4dc402f39a07ce1e0ac',
        ),
    )

    monkeypatch.setattr(
        adapter._version_service,
        'get_git_tag_version_info',
        lambda repo_dir, **kwargs: PackageVersionInfo(current_version=None, latest_version='v15.0.2', versions=['v15.0.2']),
    )

    info = adapter.get_version_info()

    assert info.current_version == 'v15.0.2'
    assert info.latest_version == 'v15.0.2'


def test_asset_action_endpoints_enqueue_tasks_and_persist_plan(tmp_path: Path, monkeypatch) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml(compose_file='compose.custom.yml'))

    from app.scanners.docker_scanner import parse_docker_ps_lines

    monkeypatch.setattr(
        'app.services.docker_versions.DockerVersionService.get_registry_tag_version_info',
        lambda self, image_repository, *, current_version=None: PackageVersionInfo(
            current_version='latest',
            latest_version='latest',
            versions=['latest'],
        ),
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
    command = update_payload['plan']['commands'][0]
    assert command[1:3] == ['-m', 'app.services.docker_lifecycle']
    context = json.loads(command[3])
    assert context['project'] == 'cliproxyapi'
    assert context['compose_files'] == ['compose.custom.yml']
    assert command[4] == 'update_latest'

    deploy_response = client.post('/api/assets/cpa/actions/deploy-version', json={'version': 'v1.2.3'})
    assert deploy_response.status_code == 202
    deploy_payload = deploy_response.json()
    assert deploy_payload['task']['requested_version'] == 'v1.2.3'
    assert deploy_payload['plan']['preview_objects'] == ['eceasy/cli-proxy-api:v1.2.3']

    delete_response = client.post('/api/assets/cpa/actions/delete')
    assert delete_response.status_code == 202
    delete_command = delete_response.json()['plan']['commands'][0]
    assert delete_command[1:3] == ['-m', 'app.services.docker_lifecycle']
    assert delete_command[4] == 'delete'

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
    assert len(payload['plan']['commands']) == 1
    command = payload['plan']['commands'][0]
    assert command[1:3] == ['-m', 'app.services.docker_lifecycle']
    assert command[4:] == ['deploy_version', 'v9.9.9']
    assert json.loads(command[3])['image_repository'] == 'eceasy/cli-proxy-api'


def test_compose_runtime_actions_control_the_entire_application_stack() -> None:
    adapter = DockerComposeAdapter(
        project_dir='/srv/full-stack-app',
        compose_file='docker-compose.local.yml',
        primary_container='app-web',
        compose_service='web',
    )

    assert adapter.plan_action('start').commands == [
        ['docker', 'compose', '-f', 'docker-compose.local.yml', 'up', '-d']
    ]
    assert adapter.plan_action('stop').commands == [
        ['docker', 'compose', '-f', 'docker-compose.local.yml', 'stop']
    ]
    assert adapter.plan_action('restart').commands == [
        ['docker', 'compose', '-f', 'docker-compose.local.yml', 'restart']
    ]
    assert adapter.plan_action('start').preview_objects == ['full compose stack']
