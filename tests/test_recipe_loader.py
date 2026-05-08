from pathlib import Path

from app.recipes.loader import load_docker_recipes


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
    assert recipe.override_file.endswith('overrides/openai-cpa.compose.override.yaml')
    assert recipe.managed_services == ['codex-web']
