from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from app.models.registry import CategoryDefinition, ObjectDefinition, RegistrySnapshot

ModelT = TypeVar('ModelT', bound=BaseModel)


def load_registry(config_root: Path) -> RegistrySnapshot:
    categories_dir = _require_directory(config_root / 'categories')
    objects_dir = _require_directory(config_root / 'objects')

    categories, category_paths = _load_categories(categories_dir)
    objects = _load_objects(objects_dir, category_paths)

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


def _load_objects(directory: Path, category_paths: dict[str, Path]) -> list[ObjectDefinition]:
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
    return objects


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
