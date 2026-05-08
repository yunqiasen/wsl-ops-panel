import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
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

    assert [asset.object_id for asset in assets] == ['cpa', 'new_api', 'idle']

    cpa_asset = assets[0]
    assert [container.name for container in cpa_asset.containers] == ['cpa-api', 'cpa-worker']
    assert cpa_asset.primary_container_name == 'cpa-api'
    assert cpa_asset.status == 'Up 3 days'
    assert cpa_asset.supports_actions == ['update_latest', 'deploy_version', 'delete', 'full_delete']

    new_api_asset = assets[1]
    assert [container.name for container in new_api_asset.containers] == ['new-api']
    assert new_api_asset.primary_container_name == 'new-api'

    idle_asset = assets[2]
    assert idle_asset.containers == []
    assert idle_asset.status == 'not running'


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
    assert 'CPA / CLIProxyAPI' in home_response.text

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
    assert '版本源：git_tags' in response.text
    assert 'Git 当前：v14.2.6' in response.text
    assert 'Git 最新：v14.2.7' in response.text
    assert '运行镜像：v14.2.6-overlay' in response.text
