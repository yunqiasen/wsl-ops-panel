from pathlib import Path

import yaml

from app.models.registry import CategoryDefinition, ObjectDefinition, RegistrySnapshot


def load_registry(config_root: Path) -> RegistrySnapshot:
    categories = []
    for path in sorted((config_root / 'categories').glob('*.yaml')):
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        categories.append(CategoryDefinition.model_validate(raw))

    objects = []
    for path in sorted((config_root / 'objects').glob('*.yaml')):
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
        objects.append(ObjectDefinition.model_validate(raw))

    return RegistrySnapshot(categories=categories, objects=objects)
