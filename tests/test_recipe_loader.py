from pathlib import Path

import yaml

from app.recipes.loader import load_docker_recipes
from app.recipes.service import DockerRecipeService


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_load_docker_recipes_reads_override_and_strategy(tmp_path: Path) -> None:
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
