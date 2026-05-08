from pathlib import Path

import yaml
from pydantic import ValidationError

from app.models.recipes import DockerRecipe


def load_docker_recipes(directory: Path) -> dict[str, DockerRecipe]:
    if not directory.is_dir():
        raise FileNotFoundError(f'Recipe directory not found: {directory}')

    recipes: dict[str, DockerRecipe] = {}
    recipe_paths: dict[str, Path] = {}
    for path in sorted(directory.glob('*.yaml')):
        recipe = _load_recipe(path)
        _ensure_unique_recipe_id(recipe.id, path, recipe_paths)
        override_path = (path.parent / recipe.override_file).resolve()
        if not override_path.is_file():
            raise FileNotFoundError(
                f"Recipe '{recipe.id}' in {path} references missing override file: {override_path}"
            )
        recipe_paths[recipe.id] = path
        recipe = recipe.model_copy(update={'override_file': str(override_path)})
        recipes[recipe.id] = recipe
    return recipes


def _load_recipe(path: Path) -> DockerRecipe:
    try:
        payload = yaml.safe_load(path.read_text(encoding='utf-8'))
        return DockerRecipe.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f'Invalid recipe definition in {path}: {exc}') from exc
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML in {path}: {exc}') from exc


def _ensure_unique_recipe_id(recipe_id: str, path: Path, seen_paths: dict[str, Path]) -> None:
    if recipe_id in seen_paths:
        existing_path = seen_paths[recipe_id]
        raise ValueError(
            f"Duplicate recipe id '{recipe_id}' in {path}; already defined in {existing_path}"
        )
