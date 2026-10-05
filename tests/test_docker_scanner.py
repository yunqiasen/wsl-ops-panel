import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.services.assets import discovered_docker_object_id
from app.models.assets import PackageVersionInfo, RuntimeVersionInfo
from app.models.registry import CategoryDefinition, ObjectDefinition, RegistrySnapshot


def _write_registry_file(root: Path, folder: str, name: str, content: str) -> None:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding='utf-8')


def _docker_object_yaml(
    *,
    object_id: str,
    name: str,
    project_dir: str,
    primary_container: str | None = None,
) -> str:
    primary = '' if primary_container is None else f'  primary_container: {primary_container}\n'
    return (
        f'id: {object_id}\n'
        'category: docker\n'
        'type: docker_compose\n'
        f'name: {name}\n'
        'config:\n'
        f'  project_dir: {project_dir}\n'
        '  compose_file: docker-compose.yml\n'
        f'{primary}'
    )


def _docker_ps_line(
    *,
    container_id: str,
    name: str,
    image: str,
    working_dir: str,
    status: str,
    state: str = 'running',
    service: str = 'app',
) -> str:
    return json.dumps(
        {
            'ID': container_id,
            'Image': image,
            'Labels': (
                'com.docker.compose.project=test-project,'
                f'com.docker.compose.project.working_dir={working_dir},'
                f'com.docker.compose.service={service}'
            ),
            'Names': name,
            'State': state,
            'Status': status,
            'Ports': '8080/tcp',
        }
    )


def _make_registry_snapshot() -> RegistrySnapshot:
    return RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='cpa',
                category='docker',
                type='docker_compose',
                name='CPA',
                config={
                    'project_dir': '/srv/cpa',
                    'compose_file': 'docker-compose.yml',
                    'primary_container': 'cpa-api',
                },
            ),
            ObjectDefinition(
                id='new_api',
                category='docker',
                type='docker_compose',
                name='New API',
                config={
                    'project_dir': '/srv/new-api',
                    'compose_file': 'docker-compose.yml',
                },
            ),
            ObjectDefinition(
                id='idle',
                category='docker',
                type='docker_compose',
                name='Idle Project',
                config={
                    'project_dir': '/srv/idle',
                    'compose_file': 'docker-compose.yml',
                },
            ),
        ],
    )


def test_parse_docker_ps_lines_extracts_name_image_tag_and_status() -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines

    snapshots = parse_docker_ps_lines(
        [
            _docker_ps_line(
                container_id='abc123',
                name='cli-proxy-api',
                image='eceasy/cli-proxy-api:latest',
                working_dir='/srv/cpa',
                status='Up 3 days (healthy)',
                service='api',
            )
        ]
    )

    assert len(snapshots) == 1
    assert snapshots[0].name == 'cli-proxy-api'
    assert snapshots[0].image_tag == 'latest'
    assert snapshots[0].status == 'Up 3 days (healthy)'
    assert snapshots[0].compose_working_dir == '/srv/cpa'
    assert snapshots[0].compose_service == 'api'


def test_parse_docker_ps_lines_skips_malformed_rows() -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines

    snapshots = parse_docker_ps_lines(
        [
            'not-json',
            json.dumps({'Image': 'alpine:3.20'}),
            _docker_ps_line(
                container_id='abc123',
                name='cli-proxy-api',
                image='eceasy/cli-proxy-api:latest',
                working_dir='/srv/cpa',
                status='Up 3 days (healthy)',
                service='api',
            ),
        ]
    )

    assert [snapshot.name for snapshot in snapshots] == ['cli-proxy-api']


def test_build_docker_asset_snapshots_groups_registry_objects_instead_of_raw_containers() -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    containers = parse_docker_ps_lines(
        [
            _docker_ps_line(
                container_id='1',
                name='cpa-api',
                image='eceasy/cli-proxy-api:latest',
                working_dir='/srv/cpa',
                status='Up 3 days',
                service='api',
            ),
            _docker_ps_line(
                container_id='2',
                name='cpa-worker',
                image='busybox:1.36',
                working_dir='/srv/cpa',
                status='Up 3 days',
                service='worker',
            ),
            _docker_ps_line(
                container_id='3',
                name='new-api',
                image='calciumion/new-api:v1',
                working_dir='/srv/new-api',
                status='Up 1 day',
                service='api',
            ),
            _docker_ps_line(
                container_id='4',
                name='untracked',
                image='alpine:3.20',
                working_dir='/srv/other',
                status='Up 1 hour',
                service='sidecar',
            ),
        ]
    )

    assets = build_docker_asset_snapshots(_make_registry_snapshot(), containers)

    assert [asset.object_id for asset in assets] == ['cpa', 'new_api', 'idle', discovered_docker_object_id('/srv/other', 'test-project')]

    cpa_asset = assets[0]
    assert [container.name for container in cpa_asset.containers] == ['cpa-api', 'cpa-worker']
    assert cpa_asset.primary_container_name == 'cpa-api'
    assert cpa_asset.status == 'Up 3 days'
    assert cpa_asset.supports_actions == [
        'update_latest',
        'deploy_version',
        'start',
        'stop',
        'restart',
        'autostart_enable',
        'autostart_disable',
        'notify_send',
        'delete',
        'full_delete',
    ]

    new_api_asset = assets[1]
    assert [container.name for container in new_api_asset.containers] == ['new-api']
    assert new_api_asset.primary_container_name == 'new-api'

    idle_asset = assets[2]
    assert idle_asset.containers == []
    assert idle_asset.status == 'not running'

    discovered_asset = assets[3]
    assert discovered_asset.metadata['discovery_source'] == 'runtime_discovered'
    assert discovered_asset.primary_container_name == 'untracked'


def test_build_docker_asset_snapshots_attach_strategy_and_recipe_metadata() -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.models.recipes import DockerRecipe
    from app.services.assets import build_docker_asset_snapshots
    from app.services.docker_versions import DockerVersionService

    containers = parse_docker_ps_lines(
        [
            _docker_ps_line(
                container_id='5',
                name='wenfxl_codex_manager',
                image='local/wenfxl-codex-manager:v14.2.6-overlay',
                working_dir='/srv/openai-cpa',
                status='Up 2 days',
                service='codex-web',
            )
        ]
    )
    openai_object = ObjectDefinition(
        id='openai_cpa',
        category='docker',
        type='docker_compose',
        name='openai-cpa',
        config={
            'project_dir': '/srv/openai-cpa',
            'compose_file': 'docker-compose.yml',
            'primary_container': 'wenfxl_codex_manager',
            'compose_service': 'codex-web',
            'lifecycle_strategy': 'compose_local_build_git_tag',
            'version_source': 'git_tags',
            'recipe_id': 'openai-cpa',
        },
    )
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[openai_object],
    )

    class StubRecipeService:
        def get(self, recipe_id: str | None) -> DockerRecipe | None:
            if recipe_id != 'openai-cpa':
                return None
            return DockerRecipe(
                id='openai-cpa',
                lifecycle_strategy='compose_local_build_git_tag',
                version_source='git_tags',
                repo_dir='/srv/openai-cpa',
                compose_file='docker-compose.yml',
                compose_service='codex-web',
                primary_container='wenfxl_codex_manager',
                override_file='/tmp/openai-cpa.compose.override.yaml',
                local_image_repository='local/wenfxl-codex-manager',
                local_image_tag_template='{version}-overlay',
            )

    class StubVersionService:
        @staticmethod
        def build_runtime_version_info(container) -> RuntimeVersionInfo:
            return DockerVersionService.build_runtime_version_info(container)

        @staticmethod
        def get_git_tag_version_info(repo_dir: str, *, fetch: bool = False):
            assert repo_dir == '/srv/openai-cpa'
            assert fetch is False
            return PackageVersionInfo(
                current_version='v14.2.6',
                latest_version='v14.2.7',
                versions=['v14.2.7', 'v14.2.6'],
                source_status='ok',
            )

    assets = build_docker_asset_snapshots(
        registry_snapshot,
        containers,
        recipe_service=StubRecipeService(),
        version_service=StubVersionService(),
    )

    assert [asset.object_id for asset in assets] == ['openai_cpa']
    openai_asset = assets[0]
    assert openai_asset.current_version == 'v14.2.6'
    assert openai_asset.latest_version == 'v14.2.7'
    assert openai_asset.metadata['recipe_id'] == 'openai-cpa'
    assert openai_asset.metadata['lifecycle_strategy'] == 'compose_local_build_git_tag'
    assert openai_asset.metadata['version_source'] == 'git_tags'
    assert openai_asset.metadata['available_versions'] == ['v14.2.7', 'v14.2.6']
    assert openai_asset.metadata['managed_services'] == []
    assert openai_asset.metadata['ignored_services'] == []
    assert openai_asset.metadata['source_status'] == 'ok'
    assert openai_asset.metadata['version_source_status'] == 'ok'
    assert openai_asset.metadata['runtime']['image_tag'] == 'v14.2.6-overlay'
    assert openai_asset.metadata['runtime_image_tag'] == 'v14.2.6-overlay'
    assert openai_asset.metadata['runtime_oci_version'] is None


def test_build_docker_asset_snapshots_prefers_runtime_oci_version_for_current_local_image() -> None:
    from app.models.recipes import DockerRecipe
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots
    from app.services.docker_versions import DockerVersionService

    raw = json.loads(
        _docker_ps_line(
            container_id='openai123',
            name='wenfxl_codex_manager',
            image='local/openai-cpa-patched:latest',
            working_dir='/srv/openai-cpa',
            status='Up 2 days',
            service='codex-web',
        )
    )
    raw['Labels'] += (
        ',org.opencontainers.image.version=15.0.2,'
        'org.opencontainers.image.revision=699df3af90380729456fe4dc402f39a07ce1e0ac'
    )
    containers = parse_docker_ps_lines([json.dumps(raw)])
    openai_object = ObjectDefinition(
        id='openai_cpa',
        category='docker',
        type='docker_compose',
        name='openai-cpa',
        config={
            'project_dir': '/srv/openai-cpa',
            'compose_file': 'docker-compose.yml',
            'primary_container': 'wenfxl_codex_manager',
            'compose_service': 'codex-web',
            'lifecycle_strategy': 'compose_local_build_git_tag',
            'version_source': 'git_tags',
            'recipe_id': 'openai-cpa',
        },
    )
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[openai_object],
    )

    class StubRecipeService:
        def get(self, recipe_id: str | None):
            if recipe_id != 'openai-cpa':
                return None
            return DockerRecipe(
                id='openai-cpa',
                lifecycle_strategy='compose_local_build_git_tag',
                version_source='git_tags',
                repo_dir='/srv/openai-cpa',
                compose_file='docker-compose.yml',
                compose_service='codex-web',
                primary_container='wenfxl_codex_manager',
                override_file='/tmp/openai-cpa.compose.override.yaml',
                local_image_repository='local/wenfxl-codex-manager',
                local_image_tag_template='{version}-overlay',
            )

    class StubVersionService:
        @staticmethod
        def build_runtime_version_info(container) -> RuntimeVersionInfo:
            return DockerVersionService.build_runtime_version_info(container)

    assets = build_docker_asset_snapshots(
        registry_snapshot,
        containers,
        recipe_service=StubRecipeService(),
        version_service=StubVersionService(),
        resolve_remote_versions=False,
    )

    assert assets[0].current_version == 'v15.0.2'
    assert assets[0].metadata['runtime_image_tag'] == 'latest'
    assert assets[0].metadata['runtime_oci_version'] == '15.0.2'


def test_category_and_detail_routes_render_docker_assets(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'cpa.yaml',
        _docker_object_yaml(
            object_id='cpa',
            name='CPA / CLIProxyAPI',
            project_dir='/srv/cpa',
            primary_container='cli-proxy-api',
        ),
    )

    from app.scanners.docker_scanner import parse_docker_ps_lines

    containers = parse_docker_ps_lines(
        [
            _docker_ps_line(
                container_id='abc123',
                name='cli-proxy-api',
                image='eceasy/cli-proxy-api:latest',
                working_dir='/srv/cpa',
                status='Up 3 days',
                service='api',
            )
        ]
    )

    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    home_response = client.get('/')
    assert home_response.status_code == 200
    assert 'Docker' in home_response.text
    assert 'CPA / CLIProxyAPI' not in home_response.text

    category_response = client.get('/categories/docker')
    assert category_response.status_code == 200
    assert 'cli-proxy-api' in category_response.text
    assert '/assets/cpa' in category_response.text

    detail_response = client.get('/assets/cpa')
    assert detail_response.status_code == 200
    assert 'CPA / CLIProxyAPI' in detail_response.text
    assert '/srv/cpa' in detail_response.text
    assert 'eceasy/cli-proxy-api:latest' in detail_response.text


def test_category_route_shows_docker_strategy_and_latest_version(tmp_path: Path, monkeypatch) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
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

    from app.scanners.docker_scanner import parse_docker_ps_lines

    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"local/wenfxl-codex-manager:v14.2.6-overlay","Labels":"com.docker.compose.project=openai-cpa,com.docker.compose.project.working_dir=/srv/openai-cpa,com.docker.compose.service=codex-web","Names":"wenfxl_codex_manager","State":"running","Status":"Up 2 days","Ports":"8128/tcp"}'
        ]
    )

    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'openai-cpa' in response.text
    assert '/assets/openai_cpa' in response.text
    assert 'compose_local_build_git_tag' in response.text
    assert 'openai-cpa' in response.text
    assert 'compose_local_build_git_tag' in response.text
    assert 'git_tags' in response.text
    assert 'v14.2.6-overlay' in response.text


def test_category_route_skips_registry_lookup_for_docker_list_page(tmp_path: Path, monkeypatch) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'cpa.yaml',
        _docker_object_yaml(
            object_id='cpa',
            name='CPA / CLIProxyAPI',
            project_dir='/srv/cpa',
            primary_container='cli-proxy-api',
        ),
    )

    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.docker_versions import DockerVersionService

    def _fail_registry_lookup(self, image_repository: str, current_version: str | None = None, *, fetcher=None):
        raise AssertionError('docker category page should not hit registry tag lookup')

    monkeypatch.setattr(DockerVersionService, 'get_registry_tag_version_info', _fail_registry_lookup)

    containers = parse_docker_ps_lines(
        [
            _docker_ps_line(
                container_id='abc123',
                name='cli-proxy-api',
                image='eceasy/cli-proxy-api:latest',
                working_dir='/srv/cpa',
                status='Up 3 days',
                service='cli-proxy-api',
            )
        ]
    )

    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'CPA / CLIProxyAPI' in response.text
    assert 'deferred' in response.text


def test_build_docker_asset_snapshots_adds_runtime_discovered_projects(tmp_path: Path) -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    repo_dir = tmp_path / 'discovered-app'
    repo_dir.mkdir()
    (repo_dir / 'docker-compose.yml').write_text('services:\n  web:\n    image: ghcr.io/example/discovered:v1\n', encoding='utf-8')
    (repo_dir / '.git').mkdir()
    (repo_dir / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    (repo_dir / '.git' / 'config').write_text(
        '[remote "origin"]\n\turl = https://github.com/example/discovered.git\n',
        encoding='utf-8',
    )
    head_ref = repo_dir / '.git' / 'refs' / 'heads'
    head_ref.mkdir(parents=True)
    (head_ref / 'main').write_text('abcdef1234567890\n', encoding='utf-8')

    registered_dir = tmp_path / 'registered'
    registered_dir.mkdir()
    containers = parse_docker_ps_lines(
        [
            _docker_ps_line(
                container_id='1',
                name='registered-api',
                image='example/registered:latest',
                working_dir=str(registered_dir),
                status='Up 1 hour',
                service='api',
            ),
            _docker_ps_line(
                container_id='2',
                name='discovered-web',
                image='ghcr.io/example/discovered:v1',
                working_dir=str(repo_dir),
                status='Up 2 hours',
                service='web',
            ),
        ]
    )
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='registered',
                category='docker',
                type='docker_compose',
                name='Registered',
                config={
                    'project_dir': str(registered_dir),
                    'compose_file': 'docker-compose.yml',
                    'compose_service': 'api',
                },
            )
        ],
    )

    assets = build_docker_asset_snapshots(registry_snapshot, containers, resolve_remote_versions=False)

    assert [asset.object_id for asset in assets] == ['registered', discovered_docker_object_id(repo_dir, 'test-project')]
    discovered = assets[1]
    assert discovered.name == 'discovered-app'
    assert discovered.supports_actions == [
        'update_latest',
        'deploy_version',
        'start',
        'stop',
        'restart',
        'autostart_enable',
        'autostart_disable',
        'notify_send',
        'delete',
        'full_delete',
    ]
    assert discovered.metadata['discovery_source'] == 'runtime_discovered'
    assert discovered.metadata['project_dir'] == str(repo_dir)
    assert discovered.metadata['compose_file'] == 'docker-compose.yml'
    assert discovered.metadata['compose_service'] == 'web'
    assert discovered.metadata['image_repository'] == 'ghcr.io/example/discovered'
    assert discovered.metadata['git_remote_url'] == 'https://github.com/example/discovered.git'
    assert discovered.metadata['git_branch'] == 'main'
    assert discovered.metadata['head_sha'] == 'abcdef1234567890'
    assert discovered.metadata['capabilities']['repo_metadata']['enabled'] is True
    assert discovered.metadata['capabilities']['cf_tunnel']['enabled'] is False


def test_registered_searxng_uses_real_project_name_and_source_links(tmp_path: Path) -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = tmp_path / 'searxng-mcp'
    project_dir.mkdir()
    (project_dir / 'docker-compose.yaml').write_text('services:\n  searxng:\n    image: searxng/searxng:latest\n', encoding='utf-8')
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='searxng',
                category='docker',
                type='docker_compose',
                name='SearXNG',
                config={
                    'project_dir': str(project_dir),
                    'compose_file': 'docker-compose.yaml',
                    'primary_container': 'searxng',
                    'compose_service': 'searxng',
                },
            )
        ],
    )
    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"searxng/searxng:latest",'
            f'"Labels":"com.docker.compose.project=searxng,com.docker.compose.project.working_dir={project_dir},'
            'com.docker.compose.service=searxng,org.opencontainers.image.source=https://github.com/searxng/searxng,'
            'org.opencontainers.image.url=https://searxng.org",'
            '"Names":"searxng","State":"running","Status":"Up 1 hour","Ports":"8080/tcp"}'
        ]
    )

    assets = build_docker_asset_snapshots(registry_snapshot, containers, resolve_remote_versions=False)

    assert [asset.object_id for asset in assets] == ['searxng']
    assert assets[0].name == 'SearXNG'
    assert assets[0].metadata['source_links']['github'] == 'https://github.com/searxng/searxng'
    assert assets[0].metadata['source_links']['docker'] == 'https://hub.docker.com/r/searxng/searxng'


def test_registered_docker_asset_uses_local_git_remote_for_source_links(tmp_path: Path) -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = tmp_path / 'cpa'
    project_dir.mkdir()
    (project_dir / '.git').mkdir()
    (project_dir / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    (project_dir / '.git' / 'config').write_text('[remote "origin"]\n\turl = https://github.com/router-for-me/CLIProxyAPI.git\n', encoding='utf-8')
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='cpa',
                category='docker',
                type='docker_compose',
                name='CPA',
                config={
                    'project_dir': str(project_dir),
                    'compose_file': 'docker-compose.yml',
                    'compose_service': 'cli-proxy-api',
                },
            )
        ],
    )
    containers = parse_docker_ps_lines([_docker_ps_line(container_id='1', name='cli-proxy-api', image='eceasy/cli-proxy-api:latest', working_dir=str(project_dir), status='Up 1 hour', service='cli-proxy-api')])

    assets = build_docker_asset_snapshots(registry_snapshot, containers, resolve_remote_versions=False)

    assert assets[0].metadata['source_links']['github'] == 'https://github.com/router-for-me/CLIProxyAPI'


def test_registered_docker_asset_includes_cf_capability_and_clean_ports(tmp_path: Path) -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = tmp_path / 'cpa'
    (project_dir / 'scripts').mkdir(parents=True)
    (project_dir / 'logs').mkdir()
    (project_dir / 'scripts' / 'cftunnel-start.sh').write_text('#!/usr/bin/env bash\n', encoding='utf-8')
    (project_dir / 'logs' / 'cftunnel-domain.txt').write_text('https://demo.trycloudflare.com\n', encoding='utf-8')
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='cpa',
                category='docker',
                type='docker_compose',
                name='CPA',
                config={
                    'project_dir': str(project_dir),
                    'compose_file': 'docker-compose.yml',
                    'compose_service': 'cli-proxy-api',
                },
            )
        ],
    )
    raw = json.loads(_docker_ps_line(container_id='1', name='cli-proxy-api', image='eceasy/cli-proxy-api:latest', working_dir=str(project_dir), status='Up 1 hour', service='cli-proxy-api'))
    raw['Ports'] = '0.0.0.0:8317->8317/tcp, [::]:8317->8317/tcp, 0.0.0.0:11451->11451/tcp'
    containers = parse_docker_ps_lines([json.dumps(raw)])

    assets = build_docker_asset_snapshots(registry_snapshot, containers, resolve_remote_versions=False)

    assert assets[0].metadata['capabilities']['cf_tunnel']['enabled'] is True
    assert assets[0].metadata['capabilities']['cf_tunnel']['current_url'] == 'https://demo.trycloudflare.com'
    assert assets[0].metadata['display_ports'] == '8317/tcp, 11451/tcp'
    assert assets[0].metadata['primary_public_port'] == '8317'


def test_registered_docker_asset_uses_configured_business_endpoints(tmp_path: Path) -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = tmp_path / 'cpa'
    project_dir.mkdir()
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='cpa',
                category='docker',
                type='docker_compose',
                name='CPA',
                config={
                    'project_dir': str(project_dir),
                    'compose_file': 'docker-compose.yml',
                    'compose_service': 'cli-proxy-api',
                    'endpoints': [
                        {
                            'label': 'API / 管理面板入口',
                            'host_port': '8317',
                            'container_port': '8317',
                            'path': '/management.html',
                            'primary': True,
                        },
                        {
                            'label': 'Antigravity OAuth 回调',
                            'host_port': '51121',
                            'container_port': '51121',
                            'path': '/oauth-callback',
                        },
                    ],
                },
            )
        ],
    )
    raw = json.loads(_docker_ps_line(container_id='1', name='cli-proxy-api', image='eceasy/cli-proxy-api:latest', working_dir=str(project_dir), status='Up 1 hour', service='cli-proxy-api'))
    raw['Ports'] = '0.0.0.0:8317->8317/tcp, 0.0.0.0:51121->51121/tcp, 0.0.0.0:54545->54545/tcp'
    containers = parse_docker_ps_lines([json.dumps(raw)])

    assets = build_docker_asset_snapshots(registry_snapshot, containers, resolve_remote_versions=False)

    assert assets[0].metadata['primary_url'] == 'http://100.126.43.55:8317/management.html'
    assert assets[0].metadata['service_endpoints'][1]['tailscale_url'] == 'http://100.126.43.55:51121/oauth-callback'
    assert 'Antigravity OAuth 回调：http://100.126.43.55:51121/oauth-callback' in assets[0].metadata['endpoint_lines']


def test_runtime_discovered_docker_asset_respects_deleted_tombstone(tmp_path: Path) -> None:
    from app.models.registry import RegistrySnapshot
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    repo_dir = tmp_path / 'image2api'
    repo_dir.mkdir()
    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"ghcr.io/basketikun/chatgpt2api:latest",'
            f'"Labels":"com.docker.compose.project=image2api,com.docker.compose.project.working_dir={repo_dir},com.docker.compose.service=image2api",'
            '"Names":"image2api","State":"running","Status":"Up 1 hour","Ports":"8312/tcp"}'
        ]
    )

    assets = build_docker_asset_snapshots(
        RegistrySnapshot(categories=[], objects=[]),
        containers,
        deleted_asset_ids={discovered_docker_object_id(repo_dir, 'image2api')},
    )

    assert assets == []



def test_runtime_discovered_docker_asset_uses_friendly_display_name_and_description(tmp_path: Path) -> None:
    from app.models.registry import RegistrySnapshot
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = Path('/home/div/1_Project_dir/regmail-2api/资源')
    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"local/wenfxl-codex-manager:v15.0.3-overlay",'
            f'"Labels":"com.docker.compose.project=openai-cpa-5-mod,com.docker.compose.project.working_dir={project_dir},com.docker.compose.service=openai-cpa-5-mod,org.opencontainers.image.source=https://github.com/wenfxl/openai-cpa",'
            '"Names":"openai_cpa_5_mod","State":"running","Status":"Up 1 hour","Ports":"0.0.0.0:8132->8000/tcp"}'
        ]
    )

    assets = build_docker_asset_snapshots(RegistrySnapshot(categories=[], objects=[]), containers, resolve_remote_versions=False)

    assert assets[0].object_id == discovered_docker_object_id(project_dir, 'openai-cpa-5-mod')
    assert assets[0].name == 'OpenAI-cpa-5'
    assert assets[0].metadata['description'].startswith('OpenAI-cpa-5 实例')


def test_runtime_discovered_image2api_uses_standalone_card_name() -> None:
    from app.models.registry import RegistrySnapshot
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = Path('/home/div/1_Project_dir/AI/image/image2api/deploy')
    containers = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"mysql:8.0",'
            f'"Labels":"com.docker.compose.project=image2api,com.docker.compose.project.working_dir={project_dir},com.docker.compose.service=mysql",'
            '"Names":"image2api-mysql","State":"running","Status":"Up 1 hour","Ports":"3306/tcp"}'
        ]
    )

    assets = build_docker_asset_snapshots(RegistrySnapshot(categories=[], objects=[]), containers, resolve_remote_versions=False)

    assert assets[0].name == 'image2api 后端依赖栈'
    assert ' / ' not in assets[0].name
    assert assets[0].metadata['description'].startswith('image2api 后端依赖栈：运行 MySQL 与 Redis')

def test_runtime_discovered_docker_assets_disambiguate_same_directory_names(tmp_path: Path) -> None:
    from app.models.registry import RegistrySnapshot
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    first = tmp_path / 'image2api' / 'deploy'
    second = tmp_path / 'studio' / 'deploy'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / 'docker-compose.yml').write_text('services:\n  web:\n    image: nginx:alpine\n', encoding='utf-8')
    (second / 'docker-compose.yml').write_text('services:\n  web:\n    image: nginx:alpine\n', encoding='utf-8')

    containers = parse_docker_ps_lines([
        _docker_ps_line(container_id='1', name='first-web', image='nginx:alpine', working_dir=str(first), status='Up 1 hour', service='web'),
        _docker_ps_line(container_id='2', name='second-web', image='nginx:alpine', working_dir=str(second), status='Up 2 hours', service='web'),
    ])

    assets = build_docker_asset_snapshots(RegistrySnapshot(categories=[], objects=[]), containers, resolve_remote_versions=False)

    assert len(assets) == 2
    assert len({asset.object_id for asset in assets}) == 2
    assert assets[0].object_id == discovered_docker_object_id(first, 'test-project')
    assert assets[1].object_id.startswith('docker__deploy-')
    assert {asset.metadata['project_dir'] for asset in assets} == {str(first), str(second)}


def test_docker_version_info_falls_back_to_oci_source_tags_after_ghcr_auth_error(tmp_path: Path, monkeypatch) -> None:
    from app.models.registry import RegistrySnapshot
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots
    from app.services.docker_versions import DockerVersionService

    project_dir = tmp_path / 'team-manage-refresh'
    project_dir.mkdir()
    registry_snapshot = RegistrySnapshot(categories=[], objects=[])
    raw = json.loads(_docker_ps_line(
        container_id='1',
        name='team-manage-app',
        image='ghcr.io/lolollipop/team-manage-refresh:latest',
        working_dir=str(project_dir),
        status='Up 1 hour',
        service='app',
    ))
    raw['Labels'] += ',org.opencontainers.image.source=https://github.com/loLollipop/team-manage-refresh,org.opencontainers.image.version=0.2.5'

    def fake_registry(self, image_repository, current_version=None, *, source_url=None, fetcher=None, runner=None):
        assert image_repository == 'ghcr.io/lolollipop/team-manage-refresh'
        assert current_version == '0.2.5'
        assert source_url == 'https://github.com/loLollipop/team-manage-refresh'
        return PackageVersionInfo(
            current_version='0.2.5',
            latest_version='v0.2.5',
            versions=['v0.2.5'],
            source_status='ok',
            version_source='github_tags',
        )

    monkeypatch.setattr(DockerVersionService, 'get_registry_tag_version_info', fake_registry)

    assets = build_docker_asset_snapshots(
        registry_snapshot,
        parse_docker_ps_lines([json.dumps(raw)]),
        resolve_remote_versions=True,
    )

    assert assets[0].current_version == '0.2.5'
    assert assets[0].latest_version == 'v0.2.5'
    assert assets[0].metadata['version_source'] == 'github_tags'
    assert assets[0].metadata['source_status'] == 'ok'

def test_registered_docker_asset_prefers_configured_description_over_package_json_and_labels(tmp_path: Path) -> None:
    from app.scanners.docker_scanner import parse_docker_ps_lines
    from app.services.assets import build_docker_asset_snapshots

    project_dir = tmp_path / 'searxng-mcp'
    project_dir.mkdir()
    (project_dir / 'package.json').write_text(
        '{"name":"searxng-mcp","description":"SearXNG MCP Server - 自部署搜索引擎 MCP 服务"}\n',
        encoding='utf-8',
    )
    registry_snapshot = RegistrySnapshot(
        categories=[CategoryDefinition(id='docker', label='Docker', order=10)],
        objects=[
            ObjectDefinition(
                id='searxng',
                category='docker',
                type='docker_compose',
                name='SearXNG',
                description='自部署 SearXNG 元搜索引擎服务，提供网页搜索入口。',
                config={
                    'project_dir': str(project_dir),
                    'compose_file': 'docker-compose.yaml',
                    'primary_container': 'searxng',
                    'compose_service': 'searxng',
                },
            )
        ],
    )
    raw = json.loads(_docker_ps_line(container_id='1', name='searxng', image='searxng/searxng:latest', working_dir=str(project_dir), status='Up 1 hour', service='searxng'))
    raw['Labels'] += ',org.opencontainers.image.description=OCI 层的描述'

    assets = build_docker_asset_snapshots(registry_snapshot, parse_docker_ps_lines([json.dumps(raw)]), resolve_remote_versions=False)

    assert assets[0].metadata['description'] == '自部署 SearXNG 元搜索引擎服务，提供网页搜索入口。'


def test_docker_detail_page_renders_project_description(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'searxng.yaml',
        'id: searxng\ncategory: docker\ntype: docker_compose\nname: SearXNG\n'
        'description: 自部署 SearXNG 元搜索引擎服务，提供网页搜索入口。\n'
        'config:\n  project_dir: /srv/searxng\n  compose_file: docker-compose.yaml\n'
        '  primary_container: searxng\n  compose_service: searxng\n',
    )
    from app.scanners.docker_scanner import parse_docker_ps_lines

    containers = parse_docker_ps_lines([
        _docker_ps_line(container_id='1', name='searxng', image='searxng/searxng:latest', working_dir='/srv/searxng', status='Up 1 hour', service='searxng')
    ])
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: containers))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/assets/searxng')

    assert response.status_code == 200
    assert 'asset-hero-description' in response.text
    assert '自部署 SearXNG 元搜索引擎服务，提供网页搜索入口。' in response.text
