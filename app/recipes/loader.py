from pathlib import Path

import yaml
from pydantic import ValidationError

from app.models.recipes import DockerRecipe


def load_docker_recipes(directory: Path, recipe_ids: set[str] | None = None) -> dict[str, DockerRecipe]:
    if not directory.is_dir():
        raise FileNotFoundError(f'Recipe directory not found: {directory}')

    recipes: dict[str, DockerRecipe] = {}
    recipe_paths: dict[str, Path] = {}
    for path in sorted(directory.glob('*.yaml')):
        recipe = _load_recipe(path, recipe_ids=recipe_ids)
        if recipe is None:
            continue
        _ensure_unique_recipe_id(recipe.id, path, recipe_paths)
        override_path = (path.parent / recipe.override_file).resolve()
        if not override_path.is_file():
            raise FileNotFoundError(
                f"Recipe '{recipe.id}' in {path} references missing override file: {override_path}"
            )
        recipe_paths[recipe.id] = path
        recipe = recipe.model_copy(update={'override_file': str(override_path)})
        recipes[recipe.id] = recipe
    if recipe_ids is not None:
        _ensure_requested_recipe_ids_resolved(directory, recipe_ids, recipes)
    return recipes


def _load_recipe(path: Path, *, recipe_ids: set[str] | None = None) -> DockerRecipe | None:
    if recipe_ids is None:
        return _validate_recipe(path, _load_recipe_payload(path))

    try:
        payload = yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        if path.stem in recipe_ids:
            raise ValueError(f'Invalid YAML in {path}: {exc}') from exc
        return None

    if not isinstance(payload, dict):
        if path.stem in recipe_ids:
            return _validate_recipe(path, payload)
        return None

    recipe_id = payload.get('id')
    if not isinstance(recipe_id, str) or recipe_id not in recipe_ids:
        return None
    return _validate_recipe(path, payload)


def _load_recipe_payload(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding='utf-8'))
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML in {path}: {exc}') from exc


def _validate_recipe(path: Path, payload: object) -> DockerRecipe:
    try:
        return DockerRecipe.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f'Invalid recipe definition in {path}: {exc}') from exc


def _ensure_requested_recipe_ids_resolved(
    directory: Path,
    requested_recipe_ids: set[str],
    recipes: dict[str, DockerRecipe],
) -> None:
    missing_recipe_ids = requested_recipe_ids - recipes.keys()
    if not missing_recipe_ids:
        return

    for recipe_id in sorted(missing_recipe_ids):
        candidate_path = directory / f'{recipe_id}.yaml'
        if candidate_path.is_file():
            _validate_recipe(candidate_path, _load_recipe_payload(candidate_path))
    raise ValueError(f'Docker recipes not found for referenced recipe ids: {sorted(missing_recipe_ids)}')


def _ensure_unique_recipe_id(recipe_id: str, path: Path, seen_paths: dict[str, Path]) -> None:
    if recipe_id in seen_paths:
        existing_path = seen_paths[recipe_id]
        raise ValueError(
            f"Duplicate recipe id '{recipe_id}' in {path}; already defined in {existing_path}"
        )
