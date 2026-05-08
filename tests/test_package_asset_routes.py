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
        lambda self, repo_dir, *, runner=None, fetch=False: PackageVersionInfo(
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

    def _stub_get_git_tags(self, repo_dir, *, runner=None, fetch=False) -> PackageVersionInfo:
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
