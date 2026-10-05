from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
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


def test_agent_managed_node_package_can_update_and_load_versions_but_not_delete(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n'
        '  - name: "@anthropic-ai/claude-code"\n'
        '    managed_by: agent\n'
        '    allowed_actions: [update_latest, deploy_version]\n'
        '    blocked_reason: 保留给 Agent 专项维护\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    asset = parse_npm_package('@anthropic-ai/claude-code@2.1.112')
    store = InMemoryTaskStore()
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=store))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    from app.api import assets as assets_api

    monkeypatch.setattr(
        assets_api.NodePackageAdapter,
        'get_version_info',
        lambda self: PackageVersionInfo(
            current_version='2.1.112',
            latest_version='2.1.143',
            versions=['2.1.143', '2.1.112'],
            source_status='ok',
        ),
    )

    versions = client.get(f'/api/assets/{asset.object_id}/versions')
    update = client.post(f'/api/assets/{asset.object_id}/actions/update-latest')
    delete = client.post(f'/api/assets/{asset.object_id}/actions/delete')

    assert versions.status_code == 200
    assert versions.json()['latest_version'] == '2.1.143'
    assert update.status_code == 202
    assert update.json()['plan']['commands'] == [['npm', 'install', '-g', '@anthropic-ai/claude-code@latest']]
    assert delete.status_code == 400
    assert 'delete' in delete.json()['detail']


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


def test_docker_versions_route_returns_strategy_runtime_and_recipe_services(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'objects' / 'openai-cpa.yaml').write_text(
        'id: openai_cpa\ncategory: docker\ntype: docker_compose\nname: openai-cpa\nconfig:\n'
        '  project_dir: /srv/openai-cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: wenfxl_codex_manager\n  compose_service: codex-web\n'
        '  lifecycle_strategy: compose_local_build_git_tag\n  version_source: git_tags\n  recipe_id: openai-cpa\n'
        '  managed_services: [codex-web]\n  ignored_services: [watchtower]\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml').write_text(
        'id: openai-cpa\nlifecycle_strategy: compose_local_build_git_tag\nversion_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\ncompose_file: docker-compose.yml\ncompose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\noverride_file: overrides/openai-cpa.compose.override.yaml\n'
        'managed_services: [codex-web]\nignored_services: [watchtower]\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker' / 'overrides').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'overrides' / 'openai-cpa.compose.override.yaml').write_text(
        'services:\n  codex-web:\n    image: ${WSL_OPS_IMAGE}\n',
        encoding='utf-8',
    )

    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.docker_versions import DockerVersionService

    monkeypatch.setattr(
        DockerVersionService,
        'get_git_tag_version_info',
        lambda self, repo_dir, *, runner=None, fetch=False, remote=False: PackageVersionInfo(
            current_version='v14.2.6',
            latest_version='v14.2.7',
            versions=['v14.2.7', 'v14.2.6'],
            source_status='ok',
        ),
    )

    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"local/wenfxl-codex-manager:v14.2.6-overlay","Labels":"com.docker.compose.project=openai-cpa,com.docker.compose.project.working_dir=/srv/openai-cpa,com.docker.compose.service=codex-web,org.opencontainers.image.version=14.2.4,org.opencontainers.image.revision=ece08961","Names":"wenfxl_codex_manager","State":"running","Status":"Up 2 days","Ports":"8128/tcp"}'
        ]
    )
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/api/assets/openai_cpa/versions')

    assert response.status_code == 200
    payload = response.json()
    assert payload['current_version'] == 'v14.2.6'
    assert payload['latest_version'] == 'v14.2.7'
    assert payload['versions'] == ['v14.2.7', 'v14.2.6']
    assert payload['lifecycle_strategy'] == 'compose_local_build_git_tag'
    assert payload['version_source'] == 'git_tags'
    assert payload['managed_services'] == ['codex-web']
    assert payload['ignored_services'] == ['watchtower']
    assert payload['runtime'] == {
        'image': 'local/wenfxl-codex-manager:v14.2.6-overlay',
        'image_tag': 'v14.2.6-overlay',
        'oci_version': '14.2.4',
        'oci_revision': 'ece08961',
        'ports': '8128/tcp',
    }


def test_docker_versions_route_reuses_snapshot_git_tag_result(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'objects' / 'openai-cpa.yaml').write_text(
        'id: openai_cpa\ncategory: docker\ntype: docker_compose\nname: openai-cpa\nconfig:\n'
        '  project_dir: /srv/openai-cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: wenfxl_codex_manager\n  compose_service: codex-web\n'
        '  lifecycle_strategy: compose_local_build_git_tag\n  version_source: git_tags\n  recipe_id: openai-cpa\n'
        '  managed_services: [codex-web]\n  ignored_services: [watchtower]\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml').write_text(
        'id: openai-cpa\nlifecycle_strategy: compose_local_build_git_tag\nversion_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\ncompose_file: docker-compose.yml\ncompose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\noverride_file: overrides/openai-cpa.compose.override.yaml\n'
        'managed_services: [codex-web]\nignored_services: [watchtower]\n',
        encoding='utf-8',
    )
    overrides_dir = tmp_path / 'recipes' / 'docker' / 'overrides'
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / 'openai-cpa.compose.override.yaml').write_text(
        'services:\n  codex-web:\n    image: ${WSL_OPS_IMAGE}\n',
        encoding='utf-8',
    )

    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.docker_versions import DockerVersionService

    call_counter = {'count': 0}

    def _stub_get_git_tags(self, repo_dir, *, runner=None, fetch=False, remote=False) -> PackageVersionInfo:
        call_counter['count'] += 1
        return PackageVersionInfo(
            current_version='v14.2.6',
            latest_version='v14.2.7',
            versions=['v14.2.7', 'v14.2.6'],
            source_status='ok',
        )

    monkeypatch.setattr(DockerVersionService, 'get_git_tag_version_info', _stub_get_git_tags)

    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"local/wenfxl-codex-manager:v14.2.6-overlay","Labels":"com.docker.compose.project=openai-cpa,com.docker.compose.project.working_dir=/srv/openai-cpa,com.docker.compose.service=codex-web,org.opencontainers.image.version=14.2.4,org.opencontainers.image.revision=ece08961","Names":"wenfxl_codex_manager","State":"running","Status":"Up 2 days","Ports":"8128/tcp"}'
        ]
    )
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/api/assets/openai_cpa/versions')

    assert response.status_code == 200
    assert response.json()['versions'] == ['v14.2.7', 'v14.2.6']
    assert call_counter['count'] == 1


def test_registry_docker_versions_route_fetches_on_demand_when_list_snapshot_is_deferred(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'objects' / 'cpa.yaml').write_text(
        'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA / CLIProxyAPI\nconfig:\n'
        '  project_dir: /srv/cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: cli-proxy-api\n  compose_service: cli-proxy-api\n',
        encoding='utf-8',
    )

    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.docker_versions import DockerVersionService

    call_counter = {'count': 0}

    def _stub_registry_lookup(self, image_repository: str, current_version: str | None = None, *, fetcher=None):
        call_counter['count'] += 1
        return PackageVersionInfo(
            current_version=current_version,
            latest_version='latest',
            versions=['latest', 'nightly'],
            source_status='ok',
        )

    monkeypatch.setattr(DockerVersionService, 'get_registry_tag_version_info', _stub_registry_lookup)

    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"eceasy/cli-proxy-api:latest","Labels":"com.docker.compose.project=cpa,com.docker.compose.project.working_dir=/srv/cpa,com.docker.compose.service=cli-proxy-api","Names":"cli-proxy-api","State":"running","Status":"Up 3 days","Ports":"8317/tcp"}'
        ]
    )
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    category_response = client.get('/categories/docker')
    assert category_response.status_code == 200
    assert 'deferred' in category_response.text
    assert call_counter['count'] == 0

    versions_response = client.get('/api/assets/cpa/versions')
    assert versions_response.status_code == 200
    assert versions_response.json()['versions'] == ['latest', 'nightly']
    assert call_counter['count'] == 1



def test_runtime_discovered_docker_versions_route_fetches_on_demand(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    project_dir = tmp_path / 'freemail-proxy'
    project_dir.mkdir()
    (project_dir / 'docker-compose.yml').write_text('services:\n  web:\n    image: nginx:alpine\n', encoding='utf-8')

    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.docker_versions import DockerVersionService

    calls: list[tuple[str, str | None]] = []

    def _stub_registry_lookup(self, image_repository: str, current_version: str | None = None, *, fetcher=None):
        calls.append((image_repository, current_version))
        return PackageVersionInfo(
            current_version=current_version,
            latest_version='1.27.0',
            versions=['1.27.0', 'alpine'],
            source_status='ok',
        )

    monkeypatch.setattr(DockerVersionService, 'get_registry_tag_version_info', _stub_registry_lookup)
    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"nginx:alpine",'
            f'"Labels":"com.docker.compose.project=freemail-proxy,com.docker.compose.project.working_dir={project_dir},com.docker.compose.service=web",'
            '"Names":"freemail-proxy","State":"running","Status":"Up 1 hour","Ports":"8421/tcp"}'
        ]
    )
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    category = client.get('/categories/docker')
    from app.services.assets import discovered_docker_object_id
    object_id = discovered_docker_object_id(project_dir, 'freemail-proxy')
    response = client.get(f'/api/assets/{object_id}/versions')

    assert category.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload['current_version'] == 'alpine'
    assert payload['latest_version'] == '1.27.0'
    assert payload['versions'] == ['1.27.0', 'alpine']
    assert payload['version_source'] == 'registry_tags'
    assert calls == [('nginx', 'alpine')]

def test_reload_registry_rebuilds_recipe_and_asset_services_for_new_openai_cpa(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.docker_versions import DockerVersionService

    git_tag_calls: list[tuple[str, bool, bool]] = []

    def _stub_get_git_tags(self, repo_dir, *, runner=None, fetch=False, remote=False) -> PackageVersionInfo:
        git_tag_calls.append((repo_dir, fetch, remote))
        return PackageVersionInfo(
            current_version='v14.2.6',
            latest_version='v14.2.7',
            versions=['v14.2.7', 'v14.2.6'],
            source_status='ok',
        )

    monkeypatch.setattr(DockerVersionService, 'get_git_tag_version_info', _stub_get_git_tags)

    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"local/wenfxl-codex-manager:v14.2.6-overlay","Labels":"com.docker.compose.project=openai-cpa,com.docker.compose.project.working_dir=/srv/openai-cpa,com.docker.compose.service=codex-web,org.opencontainers.image.version=14.2.4,org.opencontainers.image.revision=ece08961","Names":"wenfxl_codex_manager","State":"running","Status":"Up 2 days","Ports":"8128/tcp"}'
        ]
    )
    client = TestClient(
        create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()),
        follow_redirects=False,
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    (tmp_path / 'objects' / 'openai-cpa.yaml').write_text(
        'id: openai_cpa\ncategory: docker\ntype: docker_compose\nname: openai-cpa\nconfig:\n'
        '  project_dir: /srv/openai-cpa\n  compose_file: docker-compose.yml\n'
        '  primary_container: wenfxl_codex_manager\n  compose_service: codex-web\n'
        '  lifecycle_strategy: compose_local_build_git_tag\n  version_source: git_tags\n  recipe_id: openai-cpa\n'
        '  managed_services: [codex-web]\n  ignored_services: [watchtower]\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker' / 'overrides').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml').write_text(
        'id: openai-cpa\nlifecycle_strategy: compose_local_build_git_tag\nversion_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\ncompose_file: docker-compose.yml\ncompose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\noverride_file: overrides/openai-cpa.compose.override.yaml\n'
        'managed_services: [codex-web]\nignored_services: [watchtower]\n'
        'local_image_repository: local/wenfxl-codex-manager\nlocal_image_tag_template: "{version}-overlay"\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker' / 'overrides' / 'openai-cpa.compose.override.yaml').write_text(
        'services:\n  codex-web:\n    image: ${WSL_OPS_IMAGE}\n',
        encoding='utf-8',
    )

    reload_response = client.post('/settings/reload-registry')
    assert reload_response.status_code == 302

    versions_response = client.get('/api/assets/openai_cpa/versions')
    assert versions_response.status_code == 200
    assert versions_response.json()['versions'] == ['v14.2.7', 'v14.2.6']

    action_response = client.post('/api/assets/openai_cpa/actions/update-latest')
    assert action_response.status_code == 202
    action_plan = action_response.json()['plan']
    assert action_plan['working_dir'] == '/srv/openai-cpa'
    assert action_plan['commands'][0][-1] == 'check'
    assert ['git', '-C', '/srv/openai-cpa', 'checkout', 'v14.2.7'] in action_plan['commands']
    assert next(c for c in action_plan['commands'] if c[:2] == ['docker', 'build']) == [
        'docker',
        'build',
        '-t',
        'local/wenfxl-codex-manager:v14.2.7-overlay',
        '--label',
        'org.opencontainers.image.version=14.2.7',
        '--label',
        'org.opencontainers.image.source=https://github.com/wenfxl/openai-cpa',
        '-f',
        'Dockerfile',
        '.',
    ]
    assert next(c for c in action_plan['commands'] if 'compose' in c and 'up' in c) == [
        'env',
        'WSL_OPS_IMAGE=local/wenfxl-codex-manager:v14.2.7-overlay',
        'docker',
        'compose',
        '-p',
        'openai-cpa',
        '--project-directory',
        '/srv/openai-cpa',
        '-f',
        '/srv/openai-cpa/docker-compose.yml',
        '-f',
        str((tmp_path / 'recipes' / 'docker' / 'overrides' / 'openai-cpa.compose.override.yaml').resolve()),
        'up',
        '-d',
        '--no-build',
        'codex-web',
    ]
    assert ('/srv/openai-cpa', False, True) in git_tag_calls
