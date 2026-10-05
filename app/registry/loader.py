from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.models.registry import CategoryDefinition, ObjectDefinition, RegistrySnapshot
from app.recipes.loader import load_docker_recipes

ModelT = TypeVar('ModelT', bound=BaseModel)


def load_registry(config_root: Path) -> RegistrySnapshot:
    categories_dir = _require_directory(config_root / 'categories')
    objects_dir = _require_directory(config_root / 'objects')

    categories, category_paths = _load_categories(categories_dir)
    objects, object_paths = _load_objects(objects_dir, category_paths)
    _validate_docker_recipe_references(config_root, objects, object_paths)

    return RegistrySnapshot(categories=categories, objects=objects)


def _require_directory(path: Path) -> Path:
    if not path.is_dir():
        raise FileNotFoundError(f'Registry directory not found: {path}')
    return path


def _load_categories(directory: Path) -> tuple[list[CategoryDefinition], dict[str, Path]]:
    categories: list[CategoryDefinition] = []
    category_paths: dict[str, Path] = {}
    for path in _iter_yaml_files(directory):
        category = _load_definition(path, CategoryDefinition)
        _ensure_unique_id('category', category.id, path, category_paths)
        category_paths[category.id] = path
        categories.append(category)
    return categories, category_paths


def _load_objects(
    directory: Path,
    category_paths: dict[str, Path],
) -> tuple[list[ObjectDefinition], dict[str, Path]]:
    objects: list[ObjectDefinition] = []
    object_paths: dict[str, Path] = {}
    for path in _iter_yaml_files(directory):
        obj = _load_definition(path, ObjectDefinition)
        _ensure_unique_id('object', obj.id, path, object_paths)
        if obj.category not in category_paths:
            raise ValueError(
                f"Unknown category '{obj.category}' referenced by object '{obj.id}' in {path}"
            )
        object_paths[obj.id] = path
        objects.append(obj)
    return objects, object_paths


def _iter_yaml_files(directory: Path) -> Iterable[Path]:
    return sorted(directory.glob('*.yaml'))


def _load_definition(path: Path, model_type: type[ModelT]) -> ModelT:
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        return model_type.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f'Invalid registry definition in {path}: {exc}') from exc
    except yaml.YAMLError as exc:
        raise ValueError(f'Invalid YAML in {path}: {exc}') from exc


def _ensure_unique_id(kind: str, definition_id: str, path: Path, seen_paths: dict[str, Path]) -> None:
    if definition_id in seen_paths:
        existing_path = seen_paths[definition_id]
        raise ValueError(
            f"Duplicate {kind} id '{definition_id}' in {path}; already defined in {existing_path}"
        )


def _validate_docker_recipe_references(
    config_root: Path,
    objects: list[ObjectDefinition],
    object_paths: dict[str, Path],
) -> None:
    docker_objects = [obj for obj in objects if obj.type == 'docker_compose' and obj.config.get('recipe_id')]
    if not docker_objects:
        return
    referenced_recipe_ids = {obj.config['recipe_id'] for obj in docker_objects}

    try:
        recipes = load_docker_recipes(config_root / 'recipes' / 'docker', recipe_ids=referenced_recipe_ids)
    except FileNotFoundError as exc:
        raise ValueError(
            f"Docker objects reference recipe ids {sorted(referenced_recipe_ids)}, but recipes directory is unavailable: {exc}"
        ) from exc

    for obj in docker_objects:
        recipe_id = obj.config['recipe_id']
        recipe = recipes.get(recipe_id)
        if recipe is None:
            raise ValueError(
                f"Docker object '{obj.id}' in {object_paths[obj.id]} references unknown recipe '{recipe_id}'"
            )
        _ensure_recipe_fields_match(obj, recipe_id, recipe, object_paths[obj.id])


def _ensure_recipe_fields_match(
    obj: ObjectDefinition,
    recipe_id: str,
    recipe: object,
    object_path: Path,
) -> None:
    comparisons = [
        ('compose_file', obj.config.get('compose_file'), getattr(recipe, 'compose_file')),
        ('compose_service', obj.config.get('compose_service'), getattr(recipe, 'compose_service')),
        ('primary_container', obj.config.get('primary_container'), getattr(recipe, 'primary_container')),
        ('lifecycle_strategy', obj.config.get('lifecycle_strategy'), getattr(recipe, 'lifecycle_strategy')),
        ('version_source', obj.config.get('version_source'), getattr(recipe, 'version_source')),
        ('managed_services', obj.config.get('managed_services', []), getattr(recipe, 'managed_services')),
        ('ignored_services', obj.config.get('ignored_services', []), getattr(recipe, 'ignored_services')),
        ('healthcheck_url', obj.config.get('healthcheck_url'), _recipe_healthcheck_url(recipe)),
    ]
    for field_name, object_value, recipe_value in comparisons:
        if object_value != recipe_value:
            raise ValueError(
                f"Docker object '{obj.id}' in {object_path} has {field_name}={object_value!r}, "
                f"but recipe '{recipe_id}' has {field_name}={recipe_value!r}"
            )


def _recipe_healthcheck_url(recipe: object) -> str | None:
    healthcheck = getattr(recipe, 'healthcheck')
    return None if healthcheck is None else healthcheck.url
