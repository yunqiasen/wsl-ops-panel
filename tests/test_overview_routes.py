from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.models.assets import AssetSnapshot, PackageVersionInfo
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


def _docker_object_with_endpoints_yaml() -> str:
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
        '  endpoints:\n'
        '    - label: API / 管理面板入口\n'
        '      host_port: "8317"\n'
        '      container_port: "8317"\n'
        '      path: /management.html\n'
        '      primary: true\n'
        '    - label: Antigravity OAuth 回调\n'
        '      host_port: "51121"\n'
        '      container_port: "51121"\n'
        '      path: /oauth-callback\n'
    )


def _docker_ps_line() -> str:
    return (
        '{"ID":"abc123","Image":"eceasy/cli-proxy-api:latest",'
        '"Labels":"com.docker.compose.project=test,com.docker.compose.project.working_dir=/srv/cpa,com.docker.compose.service=cli-proxy-api",'
        '"Names":"cli-proxy-api","State":"running","Status":"Up 3 days","Ports":"0.0.0.0:8317->8317/tcp, 0.0.0.0:51121->51121/tcp"}'
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
    assert 'https://unpkg.com' not in overview.text
    assert 'data-page="overview"' in overview.text
    assert '首页总览' in overview.text
    assert '代理监控' in overview.text
    assert 'Tailscale 入口' in overview.text
    assert '当前 WSL 面板的 Tailscale 内网访问地址' in overview.text
    assert 'Docker 预览' not in overview.text
    assert 'Clash / Mihomo 代理池' in overview.text
    assert 'Windows / WSL 关系' in overview.text
    assert '端口暴露面' in overview.text
    assert 'WSL 配置' in overview.text
    assert 'Node' in overview.text
    assert '任务中心' in overview.text

    category = client.get('/categories/docker')
    assert category.status_code == 200
    assert 'CPA / CLIProxyAPI' in category.text
    assert 'asset-board' in category.text
    assert 'asset-card__title' in category.text
    assert 'metric-strip' in category.text
    assert 'path-chip' in category.text

    detail = client.get('/assets/cpa')
    assert detail.status_code == 200
    assert 'https://unpkg.com' not in detail.text
    assert 'data-version-panel' in detail.text
    assert 'data-version-endpoint="/api/assets/cpa/versions"' in detail.text
    assert 'hx-post="/api/assets/cpa/actions/update-latest"' not in detail.text
    assert '项目能力' in detail.text

    tasks_page = client.get('/tasks')
    assert tasks_page.status_code == 200
    assert '任务中心' in tasks_page.text
    assert 'task-stats' in tasks_page.text
    assert '打开系统日志' in tasks_page.text

    logs_page = client.get('/logs')
    assert logs_page.status_code == 200
    assert str(logs_page.url).endswith('/terminals')
    assert '日志中心' not in overview.text

    settings_page = client.get('/settings')
    assert settings_page.status_code == 200
    assert '重载注册表' in settings_page.text


def test_asset_search_hidden_rule_overrides_card_grid_display() -> None:
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    assert '.asset-card[hidden]' in css
    assert 'display: none !important' in css


def test_docker_category_and_detail_render_business_endpoints(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_with_endpoints_yaml())

    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: parse_docker_ps_lines([_docker_ps_line()])))
    _login(client)

    category = client.get('/categories/docker')
    assert category.status_code == 200
    assert 'http://100.126.43.55:8317/management.html' in category.text
    assert 'href="http://100.126.43.55:8317/management.html"' in category.text
    assert 'Antigravity OAuth 回调' in category.text
    assert 'data-asset-search-text=' in category.text
    assert '51121/oauth-callback' in category.text
    assert 'metric-strip--four' in category.text
    assert '版本管理' in category.text
    assert '运行控制' in category.text
    assert '已开启' in category.text
    assert 'versioning</span>' not in category.text

    detail = client.get('/assets/cpa')
    assert detail.status_code == 200
    assert '业务端点' in detail.text
    assert 'API / 管理面板入口' in detail.text
    assert 'http://100.126.43.55:8317/management.html' in detail.text
    assert '原始 Docker 端口' in detail.text


def test_project_category_renders_docker_like_runtime_controls_and_endpoint(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'project.yaml', 'id: project\nlabel: Project\norder: 20\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    project = AssetSnapshot(
        object_id='project__regmail-2api',
        category='project',
        name='regmail-2api',
        status='active',
        metadata={
            'path': '/srv/regmail-2api',
            'stacks': ['python', 'git'],
            'git_branch': 'main',
            'service_unit': 'regmail-ui.service',
            'ports': '45345/tcp',
            'primary_public_port': '45345',
            'web_ui': {'enabled': True, 'port': '45345', 'url_path': '/email'},
            'capabilities': {
                'runtime_control': {'enabled': True},
                'autostart': {'enabled': True},
                'wechat_notify': {'enabled': False},
            },
        },
    )
    client = TestClient(create_app(config_root=tmp_path, project_scanner=lambda: [project]))
    _login(client)

    category = client.get('/categories/project')

    assert category.status_code == 200
    assert 'data-bulk-action="start"' in category.text
    assert 'data-bulk-action="restart"' in category.text
    assert 'data-bulk-action="autostart-enable"' in category.text
    assert 'data-bulk-action="notify-send"' in category.text
    assert 'data-card-version-panel="project__regmail-2api"' in category.text
    assert 'http://100.126.43.55:45345/email' in category.text
    assert 'href="http://100.126.43.55:45345/email"' in category.text
    assert 'metric-strip--four' in category.text
    assert '运行控制' in category.text
    assert '开机自启' in category.text


def test_settings_reload_registry_picks_up_new_category(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    client = TestClient(create_app(config_root=tmp_path), follow_redirects=False)
    _login(client)

    before = client.get('/settings')
    assert before.status_code == 200
    assert '<span>分类数</span><strong>1</strong>' in before.text

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

    def _stub_get_git_tags(self, repo_dir, *, runner=None, fetch=False, remote=False):
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
    assert '<small>版本来源</small><strong>git_tags</strong>' in response.text
    assert 'v14.2.6' in response.text
    assert 'v14.2.6-overlay' in response.text
    assert '14.2.4' in response.text
    assert 'ece08961' in response.text
    assert 'data-version-endpoint="/api/assets/openai_cpa/versions"' in response.text
    assert '正在加载版本信息…' in response.text
    assert 'value="latest"' not in response.text
    assert 'value="v14.2.6-overlay"' not in response.text
    assert git_tag_calls == []

    versions_response = client.get('/api/assets/openai_cpa/versions')
    assert versions_response.status_code == 200
    assert versions_response.json()['latest_version'] == 'v14.2.7'
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


def test_category_page_renders_bulk_controls_and_detail_is_read_only(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    containers = parse_docker_ps_lines([_docker_ps_line()])
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()))
    _login(client)

    category = client.get('/categories/docker')

    assert category.status_code == 200
    assert 'data-bulk-toolbar' in category.text
    assert 'data-bulk-toolbar' in category.text
    assert 'data-category-id="docker"' in category.text
    assert 'data-bulk-toolbar hidden' not in category.text
    assert 'data-asset-select="cpa"' in category.text
    assert 'data-card-version-select="cpa"' in category.text
    assert 'data-bulk-action="update-latest"' in category.text
    assert 'data-action-label="更新最新版"' in category.text
    assert 'data-bulk-action="deploy-version"' in category.text
    assert 'data-bulk-action="cf-refresh"' not in category.text
    assert 'data-bulk-action="notify-send"' in category.text
    assert 'data-select-all-assets' in category.text
    assert 'data-clear-selection' in category.text
    assert 'data-notification-edit="cpa"' in category.text
    assert 'data-notification-endpoint="/api/notifications/cpa"' in category.text
    assert 'card-quick-actions' in category.text
    assert '微信模板' in category.text
    assert 'data-card-select-toggle="cpa"' in category.text
    assert 'asset-card__description-panel' in category.text
    assert '简介说明' in category.text
    assert '手工纳管' in category.text
    assert 'data-notification-dialog' in category.text
    assert '可用变量' in category.text
    assert 'data-bulk-hint' in category.text

    detail = client.get('/assets/cpa')

    assert detail.status_code == 200
    assert 'action-panel' not in detail.text
    assert 'hx-post="/api/assets/cpa/actions/update-latest"' not in detail.text
    assert '项目能力' in detail.text
    assert '仓库与链接' in detail.text


def test_node_category_does_not_render_cf_or_notify_bulk_actions(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: update\n    managed_by: node\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    asset = parse_npm_package('update@0.7.4')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    _login(client)

    response = client.get('/categories/node')

    assert response.status_code == 200
    assert 'package-install-panel' in response.text
    assert 'data-package-install-selected' in response.text
    assert 'data-package-delete-selected' in response.text
    assert 'data-bulk-action="update-latest"' not in response.text
    assert 'data-bulk-action="deploy-version"' not in response.text
    assert 'data-bulk-action="cf-refresh"' not in response.text
    assert 'data-bulk-action="notify-send"' not in response.text


def test_project_category_filters_bulk_actions_to_project_asset_capabilities(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'project.yaml', 'id: project\nlabel: Project\norder: 11\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    project_asset = AssetSnapshot(
        object_id='project__wsl-ops-panel',
        category='project',
        name='wsl-ops-panel',
        status='present',
        metadata={'path': '/srv/wsl-ops-panel', 'stacks': ['python'], 'web_ui': {'enabled': False}},
    )
    client = TestClient(create_app(config_root=tmp_path, project_scanner=lambda: [project_asset], task_store=InMemoryTaskStore()))
    _login(client)

    response = client.get('/categories/project')

    assert response.status_code == 200
    assert 'data-bulk-action="update-latest"' in response.text
    assert 'data-bulk-action="deploy-version"' in response.text
    assert 'data-bulk-action="cf-create"' not in response.text
    assert 'data-bulk-action="cf-refresh"' not in response.text
    assert 'data-bulk-action="cf-disable"' not in response.text
    assert 'data-bulk-action="notify-send"' not in response.text


def test_systemd_category_renders_runtime_actions_without_cf_notify(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'systemd.yaml', 'id: systemd\nlabel: systemd\norder: 20\nenabled: true\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'panel.yaml',
        'id: panel\ncategory: systemd\ntype: systemd_unit\nname: panel\nconfig:\n  unit_name: wsl-ops-panel.service\n  working_dir: /srv/panel\n',
    )
    client = TestClient(create_app(config_root=tmp_path, systemd_scanner=lambda: []))
    _login(client)

    response = client.get('/categories/systemd')

    assert response.status_code == 200
    assert 'data-bulk-action="start"' in response.text
    assert 'data-bulk-action="stop"' in response.text
    assert 'data-bulk-action="autostart-enable"' in response.text
    assert 'data-bulk-action="cf-refresh"' not in response.text
    assert 'data-bulk-action="notify-send"' not in response.text


def test_detail_page_renders_source_links_and_notification_editor(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    containers = parse_docker_ps_lines([
        '{"ID":"abc123","Image":"eceasy/cli-proxy-api:latest",'
        '"Labels":"com.docker.compose.project=test,com.docker.compose.project.working_dir=/srv/cpa,'
        'com.docker.compose.service=cli-proxy-api,org.opencontainers.image.source=https://github.com/itseasy21/CLIProxyAPI",'
        '"Names":"cli-proxy-api","State":"running","Status":"Up 3 days","Ports":"0.0.0.0:8317->8317/tcp"}'
    ])
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers, task_store=InMemoryTaskStore()))
    _login(client)

    response = client.get('/assets/cpa')

    assert response.status_code == 200
    assert 'https://github.com/itseasy21/CLIProxyAPI' in response.text
    assert 'https://hub.docker.com/r/eceasy/cli-proxy-api' in response.text
    assert 'data-notification-panel' in response.text
    assert 'id="notification"' in response.text
    assert '微信通知内容编辑' in response.text
    assert 'data-notification-save' in response.text
    assert 'data-notification-send' in response.text


def test_host_page_renders_search_and_port_controls(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'host.yaml', 'id: host\nlabel: 宿主机进程\norder: 80\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    from app.scanners.host_process_scanner import parse_listening_socket

    host_asset = parse_listening_socket('LISTEN 0 128 0.0.0.0:8317 0.0.0.0:* users:(("docker-proxy",pid=1234,fd=7))')
    client = TestClient(create_app(config_root=tmp_path, host_process_scanner=lambda: [host_asset], task_store=InMemoryTaskStore()))
    _login(client)

    response = client.get('/categories/host')

    assert response.status_code == 200
    assert 'data-asset-search' in response.text
    assert '搜索端口 / 服务 / 作用' in response.text
    assert 'CPA / CLIProxyAPI' in response.text
    assert '关闭端口' in response.text
    assert '开启端口' in response.text
    assert 'data-host-port-action="stop"' in response.text
    assert 'data-host-port-action="start"' in response.text


def test_project_category_order_is_below_docker(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'categories', 'project.yaml', 'id: project\nlabel: Project\norder: 11\nenabled: true\n')
    _write_registry_file(tmp_path, 'categories', 'systemd.yaml', 'id: systemd\nlabel: systemd\norder: 20\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    client = TestClient(create_app(config_root=tmp_path, task_store=InMemoryTaskStore()))
    _login(client)

    response = client.get('/')

    assert response.text.index('<span>Docker</span>') < response.text.index('<span>Project</span>') < response.text.index('<span>systemd</span>')


def test_category_runtime_filter_renders_controls_and_normalized_states(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    _write_registry_file(
        tmp_path,
        'objects',
        'stopped.yaml',
        'id: stopped\ncategory: docker\ntype: docker_compose\nname: Stopped App\nconfig:\n'
        '  project_dir: /srv/stopped\n  compose_file: docker-compose.yml\n'
        '  primary_container: stopped-app\n  compose_service: stopped-app\n',
    )
    client = TestClient(
        create_app(
            config_root=tmp_path,
            docker_scanner=lambda: parse_docker_ps_lines([_docker_ps_line()]),
            task_store=InMemoryTaskStore(),
        )
    )
    _login(client)

    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'data-runtime-filter="running"' in response.text
    assert 'data-runtime-filter="stopped"' in response.text
    assert response.text.count('aria-pressed="false"') >= 2
    assert 'data-runtime-state="running"' in response.text
    assert 'data-runtime-state="stopped"' in response.text


def test_system_category_does_not_render_runtime_filter_controls(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'system.yaml', 'id: system\nlabel: 系统基础设施\norder: 90\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    asset = AssetSnapshot(object_id='system__docker', category='system', name='Docker', status='available')
    client = TestClient(
        create_app(
            config_root=tmp_path,
            system_infra_scanner=lambda: [asset],
            task_store=InMemoryTaskStore(),
        )
    )
    _login(client)

    response = client.get('/categories/system')

    assert response.status_code == 200
    assert 'data-runtime-filter=' not in response.text
    assert 'data-runtime-state="unknown"' in response.text


def test_runtime_filter_styles_include_active_and_mobile_states() -> None:
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    assert '.category-runtime-filters' in css
    assert '.runtime-filter-button--running.is-active' in css
    assert '.runtime-filter-button--stopped.is-active' in css
    assert 'grid-template-columns: repeat(2, minmax(0, 1fr))' in css



def test_category_cards_render_stable_alignment_slots(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    client = TestClient(
        create_app(
            config_root=tmp_path,
            docker_scanner=lambda: parse_docker_ps_lines([_docker_ps_line()]),
            task_store=InMemoryTaskStore(),
        )
    )
    _login(client)

    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'class="asset-card asset-card--docker"' in response.text
    assert 'asset-card__status-slot' in response.text
    assert 'asset-card__endpoint-slot' in response.text


def test_category_card_alignment_styles_reserve_desktop_slots_and_release_mobile_flow() -> None:
    css = Path('app/static/app.css').read_text(encoding='utf-8')

    assert '.asset-card__status-slot' in css
    assert '.asset-card__endpoint-slot' in css
    assert '.asset-card__description-panel {' in css
    assert '.asset-board--docker .asset-card {' in css
    assert '.asset-board--project .asset-card {' in css
    assert 'block-size: var(--asset-card-description-slot);' in css
    assert 'min-block-size: var(--asset-card-status-slot);' in css
    assert '\n  block-size: var(--asset-card-status-slot);' in css
    assert '--asset-card-status-slot: 87px;' in css
    assert 'block-size: var(--asset-card-endpoint-slot);' in css
    assert '@media (max-width: 560px)' in css
    assert 'block-size: auto;' in css
    assert 'min-block-size: 0;' in css

def test_paused_docker_asset_is_not_classified_as_running(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'paused.yaml',
        'id: paused\ncategory: docker\ntype: docker_compose\nname: Paused App\nconfig:\n'
        '  project_dir: /srv/paused\n  compose_file: docker-compose.yml\n'
        '  primary_container: paused-app\n  compose_service: paused-app\n',
    )
    paused = parse_docker_ps_lines(
        [
            '{"ID":"paused1","Image":"example/paused:latest",'
            '"Labels":"com.docker.compose.project=paused,com.docker.compose.project.working_dir=/srv/paused,com.docker.compose.service=paused-app",'
            '"Names":"paused-app","State":"paused","Status":"Up 2 hours (Paused)","Ports":""}'
        ]
    )
    client = TestClient(
        create_app(config_root=tmp_path, docker_scanner=lambda: paused, task_store=InMemoryTaskStore())
    )
    _login(client)

    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'data-runtime-state="unknown" data-card-select-toggle="paused" data-asset-id="paused"' in response.text
