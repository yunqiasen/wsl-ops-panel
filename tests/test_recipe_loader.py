from pathlib import Path

import pytest
import yaml

from app.registry.loader import load_registry
from app.recipes.loader import load_docker_recipes
from app.recipes.service import DockerRecipeService


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _write_override(path: Path) -> None:
    _write(path, 'services:\n  codex-web:\n    image: ${WSL_OPS_IMAGE}\n')


def test_load_docker_recipes_reads_override_and_strategy(tmp_path: Path) -> None:
    _write_override(tmp_path / 'recipes' / 'docker' / 'overrides' / 'openai-cpa.compose.override.yaml')
    _write(
        tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml',
        'id: openai-cpa\n'
        'lifecycle_strategy: compose_local_build_git_tag\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\n'
        'override_file: overrides/openai-cpa.compose.override.yaml\n'
        'managed_services: [codex-web]\n'
        'ignored_services: [watchtower]\n'
        'local_image_repository: local/wenfxl-codex-manager\n'
        'local_image_tag_template: "{version}-overlay"\n'
        'healthcheck:\n'
        '  url: http://127.0.0.1:8128\n'
        '  expect_status: 200\n',
    )

    recipes = load_docker_recipes(tmp_path / 'recipes' / 'docker')

    recipe = recipes['openai-cpa']
    assert recipe.lifecycle_strategy == 'compose_local_build_git_tag'
    assert recipe.version_source == 'git_tags'
    assert recipe.override_file.endswith('overrides/openai-cpa.compose.override.yaml')
    assert recipe.managed_services == ['codex-web']
    assert recipe.ignored_services == ['watchtower']
    assert recipe.healthcheck is not None
    assert recipe.healthcheck.url == 'http://127.0.0.1:8128'
    assert recipe.healthcheck.expect_status == 200


def test_load_docker_recipes_requires_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match='recipes/docker'):
        load_docker_recipes(tmp_path / 'recipes' / 'docker')


def test_load_docker_recipes_rejects_duplicate_recipe_ids(tmp_path: Path) -> None:
    directory = tmp_path / 'recipes' / 'docker'
    _write_override(directory / 'overrides' / 'first.override.yaml')
    _write_override(directory / 'overrides' / 'second.override.yaml')
    _write(
        directory / 'first.yaml',
        'id: openai-cpa\n'
        'lifecycle_strategy: compose_local_build_git_tag\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\n'
        'override_file: overrides/first.override.yaml\n',
    )
    _write(
        directory / 'second.yaml',
        'id: openai-cpa\n'
        'lifecycle_strategy: compose_local_build_git_tag\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa-copy\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager_copy\n'
        'override_file: overrides/second.override.yaml\n',
    )

    with pytest.raises(ValueError, match='Duplicate recipe id'):
        load_docker_recipes(directory)


def test_load_docker_recipes_reports_yaml_error_with_file_context(tmp_path: Path) -> None:
    recipe_path = tmp_path / 'recipes' / 'docker' / 'broken.yaml'
    _write(recipe_path, 'id: [unterminated\n')

    with pytest.raises(ValueError, match=str(recipe_path)):
        load_docker_recipes(tmp_path / 'recipes' / 'docker')


def test_load_docker_recipes_reports_schema_error_with_file_context(tmp_path: Path) -> None:
    directory = tmp_path / 'recipes' / 'docker'
    _write_override(directory / 'overrides' / 'openai-cpa.compose.override.yaml')
    recipe_path = directory / 'openai-cpa.yaml'
    _write(
        recipe_path,
        'id: openai-cpa\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\n'
        'override_file: overrides/openai-cpa.compose.override.yaml\n',
    )

    with pytest.raises(ValueError, match=str(recipe_path)):
        load_docker_recipes(directory)


def test_load_docker_recipes_rejects_missing_override_file(tmp_path: Path) -> None:
    recipe_path = tmp_path / 'recipes' / 'docker' / 'openai-cpa.yaml'
    _write(
        recipe_path,
        'id: openai-cpa\n'
        'lifecycle_strategy: compose_local_build_git_tag\n'
        'version_source: git_tags\n'
        'repo_dir: /srv/openai-cpa\n'
        'compose_file: docker-compose.yml\n'
        'compose_service: codex-web\n'
        'primary_container: wenfxl_codex_manager\n'
        'override_file: overrides/missing.override.yaml\n',
    )

    with pytest.raises(FileNotFoundError, match='missing.override.yaml'):
        load_docker_recipes(tmp_path / 'recipes' / 'docker')


def test_load_docker_recipes_reads_repo_openai_cpa_recipe_and_override() -> None:
    config_root = Path(__file__).resolve().parents[1] / 'config'

    recipes = load_docker_recipes(config_root / 'recipes' / 'docker')

    recipe = recipes['openai-cpa']
    assert recipe.id == 'openai-cpa'
    assert recipe.lifecycle_strategy == 'compose_local_build_git_tag'
    assert recipe.version_source == 'git_tags'
    assert recipe.repo_dir == '/home/div/1_Project_dir/regmail-2api/资源/openai-cpa'
    assert recipe.compose_file == 'docker-compose.yml'
    assert recipe.compose_service == 'codex-web'
    assert recipe.primary_container == 'wenfxl_codex_manager'
    assert recipe.managed_services == ['codex-web']
    assert recipe.ignored_services == ['watchtower']
    assert recipe.local_image_repository == 'local/wenfxl-codex-manager'
    assert recipe.local_image_tag_template == '{version}-overlay'
    assert recipe.healthcheck is not None
    assert recipe.healthcheck.url == 'http://127.0.0.1:8128'
    assert recipe.healthcheck.expect_status == 200
    assert recipe.full_delete_paths == ['/home/div/1_Project_dir/regmail-2api/资源/openai-cpa']

    override_path = Path(recipe.override_file)
    assert override_path == (config_root / 'recipes' / 'docker' / 'overrides' / 'openai-cpa.compose.override.yaml').resolve()

    override_payload = yaml.safe_load(override_path.read_text(encoding='utf-8'))
    assert override_payload == {
        'services': {
            'codex-web': {
                'image': '${WSL_OPS_IMAGE}',
                'ports': ['8128:8000'],
                'restart': 'unless-stopped',
                'extra_hosts': ['host.docker.internal:host-gateway'],
                'environment': {
                    'TZ': 'Asia/Shanghai',
                    'HOST_PROJECT_PATH': '/home/div/1_Project_dir/regmail-2api/资源/openai-cpa',
                },
                'volumes': ['./data:/app/data', '/var/run/docker.sock:/var/run/docker.sock'],
            }
        }
    }


def test_docker_recipe_service_get_returns_safe_copy() -> None:
    config_root = Path(__file__).resolve().parents[1] / 'config'
    service = DockerRecipeService(config_root)

    recipe = service.get('openai-cpa')
    assert recipe is not None
    assert recipe.healthcheck is not None
    recipe.managed_services.append('mutated')
    recipe.healthcheck.expect_status = 503

    fresh_recipe = service.get('openai-cpa')
    assert fresh_recipe is not None
    assert fresh_recipe.managed_services == ['codex-web']
    assert fresh_recipe.healthcheck is not None
    assert fresh_recipe.healthcheck.expect_status == 200
    assert service.get(None) is None
    assert service.get('missing') is None


def test_docker_recipe_service_require_returns_safe_copy_and_rejects_missing() -> None:
    config_root = Path(__file__).resolve().parents[1] / 'config'
    service = DockerRecipeService(config_root)

    recipe = service.require('openai-cpa')
    recipe.managed_services.append('mutated')

    fresh_recipe = service.require('openai-cpa')
    assert fresh_recipe.managed_services == ['codex-web']

    with pytest.raises(ValueError, match='missing'):
        service.require('missing')


def test_repo_openai_cpa_object_recipe_reference_resolves_and_matches_fields() -> None:
    config_root = Path(__file__).resolve().parents[1] / 'config'
    registry = load_registry(config_root)
    recipe_service = DockerRecipeService(config_root)

    obj = next(item for item in registry.objects if item.id == 'openai_cpa')
    recipe = recipe_service.require(obj.config['recipe_id'])

    assert obj.config['project_dir'] == recipe.repo_dir
    assert obj.config['compose_file'] == recipe.compose_file
    assert obj.config['compose_service'] == recipe.compose_service
    assert obj.config['primary_container'] == recipe.primary_container
    assert obj.config['lifecycle_strategy'] == recipe.lifecycle_strategy
    assert obj.config['version_source'] == recipe.version_source
