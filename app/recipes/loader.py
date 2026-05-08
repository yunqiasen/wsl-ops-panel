from pathlib import Path

import yaml

from app.models.recipes import DockerRecipe


def load_docker_recipes(directory: Path) -> dict[str, DockerRecipe]:
    recipes: dict[str, DockerRecipe] = {}
    for path in sorted(directory.glob('*.yaml')):
        payload = yaml.safe_load(path.read_text(encoding='utf-8'))
        recipe = DockerRecipe.model_validate(payload)
        recipe = recipe.model_copy(
            update={
                'override_file': str((path.parent / recipe.override_file).resolve()),
            }
        )
        recipes[recipe.id] = recipe
    return recipes
