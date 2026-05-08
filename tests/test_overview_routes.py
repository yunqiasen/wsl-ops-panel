from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.models.assets import PackageVersionInfo
from app.scanners.docker_scanner import parse_docker_ps_lines
from app.scanners.node_scanner import parse_npm_package
from app.tasks.store import InMemoryTaskStore


def _write_registry_file(root: Path, folder: str, name: str, content: str) -> None:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding='utf-8')


def _docker_object_yaml() -> str:
    return (
        'id: cpa\n'
        'category: docker\n'
        'type: docker_compose\n'
        'name: CPA / CLIProxyAPI\n'
        'config:\n'
        '  project_dir: /srv/cpa\n'
        '  compose_file: docker-compose.yml\n'
        '  primary_container: cli-proxy-api\n'
        '  compose_service: cli-proxy-api\n'
    )


def _docker_ps_line() -> str:
    return (
        '{"ID":"abc123","Image":"eceasy/cli-proxy-api:latest",'
        '"Labels":"com.docker.compose.project=test,com.docker.compose.project.working_dir=/srv/cpa,com.docker.compose.service=cli-proxy-api",'
        '"Names":"cli-proxy-api","State":"running","Status":"Up 3 days","Ports":"8317/tcp"}'
    )


def _login(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, issue_session_token())


def test_login_required_for_overview() -> None:
    client = TestClient(create_app(), follow_redirects=False)

    response = client.get('/')

    assert response.status_code == 302
    assert response.headers['location'] == '/login'


def test_app_boots_with_background_task_worker() -> None:
    app = create_app(task_store=InMemoryTaskStore())
    assert hasattr(app.state, 'task_worker')

    with TestClient(app):
        assert app.state.task_worker._thread is not None
        assert app.state.task_worker._thread.is_alive()

    assert app.state.task_worker._thread is not None
    assert not app.state.task_worker._thread.is_alive()


def test_protected_pages_render_nav_and_actions(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'categories', 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())

    containers = parse_docker_ps_lines([_docker_ps_line()])
    store = InMemoryTaskStore()
    store.insert(
        create_app(task_store=store).state.task_queue.enqueue('seed', 'update_latest')
    ) if False else None
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=store))
    _login(client)

    overview = client.get('/')
    assert overview.status_code == 200
    assert '总览' in overview.text
    assert 'Node' in overview.text
    assert '任务中心' in overview.text

    category = client.get('/categories/docker')
    assert category.status_code == 200
    assert 'CPA / CLIProxyAPI' in category.text

    detail = client.get('/assets/cpa')
    assert detail.status_code == 200
    assert 'hx-post="/api/assets/cpa/actions/update-latest"' in detail.text
    assert 'hx-post="/api/assets/cpa/actions/full-delete"' in detail.text

    tasks_page = client.get('/tasks')
    assert tasks_page.status_code == 200
    assert '任务中心' in tasks_page.text

    logs_page = client.get('/logs')
    assert logs_page.status_code == 200
    assert '日志中心' in logs_page.text

    settings_page = client.get('/settings')
    assert settings_page.status_code == 200
    assert '重载注册表' in settings_page.text


def test_settings_reload_registry_picks_up_new_category(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    client = TestClient(create_app(config_root=tmp_path), follow_redirects=False)
    _login(client)

    before = client.get('/settings')
    assert before.status_code == 200
    assert '分类数：1' in before.text

    _write_registry_file(tmp_path, 'categories', 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    reload_response = client.post('/settings/reload-registry')

    assert reload_response.status_code == 302
    assert reload_response.headers['location'] == '/settings'

    category_response = client.get('/categories/node')
    assert category_response.status_code == 200
    assert 'Node' in category_response.text


def test_hx_reload_registry_unauthenticated_returns_redirect_headers(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    client = TestClient(create_app(config_root=tmp_path), follow_redirects=False)
    response = client.post('/settings/reload-registry', headers={'HX-Request': 'true'})

    assert response.status_code == 401
    assert response.headers['hx-redirect'] == '/login'
    assert response.headers['x-login-redirect'] == '/login'


def test_node_detail_hides_action_buttons_for_protected_package(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: "@openai/codex"\n    managed_by: agent_cli\n    protected: true\n    blocked_reason: 保留给 agent cli\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')

    protected_asset = parse_npm_package('@openai/codex@0.128.0')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [protected_asset]))
    _login(client)

    response = client.get(f'/assets/{protected_asset.object_id}')

    assert response.status_code == 200
    assert '保留给 agent cli' in response.text
    assert 'hx-post="/api/assets/' not in response.text


def test_openai_cpa_detail_shows_strategy_and_runtime_versions(tmp_path: Path, monkeypatch) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'openai-cpa.yaml',
        'id: openai_cpa\n'
        'category: docker\n'
        'type: docker_compose\n'
        'name: openai-cpa\n'
        'config:\n'
        '  project_dir: /srv/openai-cpa\n'
        '  compose_file: docker-compose.yml\n'
        '  primary_container: wenfxl_codex_manager\n'
        '  compose_service: codex-web\n'
        '  lifecycle_strategy: compose_local_build_git_tag\n'
        '  version_source: git_tags\n'
        '  recipe_id: openai-cpa\n'
        '  managed_services: [codex-web]\n'
        '  ignored_services: [watchtower]\n',
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

    from app.services.docker_versions import DockerVersionService

    git_tag_calls: list[str] = []

    def _stub_get_git_tags(self, repo_dir, *, runner=None, fetch=False):
        git_tag_calls.append(repo_dir)
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
    _login(client)

    response = client.get('/assets/openai_cpa')

    assert response.status_code == 200
    assert 'compose_local_build_git_tag' in response.text
    assert '版本来源：git_tags' in response.text
    assert 'v14.2.6' in response.text
    assert 'v14.2.7' in response.text
    assert 'v14.2.6-overlay' in response.text
    assert '14.2.4' in response.text
    assert 'OCI Revision：ece08961' in response.text
    assert '完整可部署版本：v14.2.7, v14.2.6' in response.text
    assert 'value="v14.2.7"' in response.text
    assert 'value="v14.2.6"' in response.text
    assert 'value="latest"' not in response.text
    assert 'value="v14.2.6-overlay"' not in response.text
    assert git_tag_calls == ['/srv/openai-cpa']


def test_app_boots_when_unreferenced_docker_recipe_is_invalid(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    (tmp_path / 'recipes' / 'docker').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'broken.yaml').write_text('id: broken\ncompose_file: docker-compose.yml\n', encoding='utf-8')

    app = create_app(config_root=tmp_path, task_store=InMemoryTaskStore())

    assert app.state.docker_recipe_service is None


def test_app_boots_with_referenced_valid_recipe_and_unreferenced_invalid_recipe(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'openai-cpa.yaml',
        'id: openai_cpa\n'
        'category: docker\n'
        'type: docker_compose\n'
        'name: openai-cpa\n'
        'config:\n'
        '  project_dir: /srv/openai-cpa\n'
        '  compose_file: docker-compose.yml\n'
        '  primary_container: wenfxl_codex_manager\n'
        '  compose_service: codex-web\n'
        '  lifecycle_strategy: compose_local_build_git_tag\n'
        '  version_source: git_tags\n'
        '  recipe_id: openai-cpa\n'
        '  managed_services: [codex-web]\n'
        '  ignored_services: [watchtower]\n',
    )
    (tmp_path / 'recipes' / 'docker' / 'overrides').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml').write_text(
        'id: openai-cpa\n'
        'lifecycle_strategy: compose_local_build_git_tag\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\n'
        'override_file: overrides/openai-cpa.compose.override.yaml\n'
        'managed_services: [codex-web]\n'
        'ignored_services: [watchtower]\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker' / 'overrides' / 'openai-cpa.compose.override.yaml').write_text(
        'services:\n  codex-web:\n    image: ${WSL_OPS_IMAGE}\n',
        encoding='utf-8',
    )
    (tmp_path / 'recipes' / 'docker' / 'broken.yaml').write_text('id: broken\ncompose_file: docker-compose.yml\n', encoding='utf-8')

    app = create_app(config_root=tmp_path, task_store=InMemoryTaskStore())

    assert app.state.docker_recipe_service is not None
    assert app.state.docker_recipe_service.require('openai-cpa').repo_dir == '/srv/openai-cpa'
