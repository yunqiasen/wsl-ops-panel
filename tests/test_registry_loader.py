from pathlib import Path

import pytest

from app.registry.loader import load_registry
from app.registry.service import RegistryService


def _write_registry_file(root: Path, folder: str, name: str, content: str) -> None:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding='utf-8')


def test_load_registry_reads_categories_and_objects(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', 'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\n')

    registry = load_registry(tmp_path)

    assert [category.id for category in registry.categories] == ['docker']
    assert [obj.id for obj in registry.objects] == ['cpa']


@pytest.mark.parametrize('missing_dir', ['categories', 'objects'])
def test_load_registry_requires_expected_directories(tmp_path: Path, missing_dir: str) -> None:
    if missing_dir != 'categories':
        (tmp_path / 'categories').mkdir(parents=True)
    if missing_dir != 'objects':
        (tmp_path / 'objects').mkdir(parents=True)

    with pytest.raises(FileNotFoundError, match=missing_dir):
        load_registry(tmp_path)


def test_load_registry_rejects_unknown_category_reference(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\n')
    object_path = tmp_path / 'objects' / 'cpa.yaml'
    _write_registry_file(tmp_path, 'objects', object_path.name, 'id: cpa\ncategory: missing\ntype: docker_compose\nname: CPA\n')

    with pytest.raises(ValueError, match=str(object_path)):
        load_registry(tmp_path)


def test_load_registry_rejects_unknown_fields_with_file_context(tmp_path: Path) -> None:
    category_path = tmp_path / 'categories' / 'docker.yaml'
    _write_registry_file(tmp_path, 'categories', category_path.name, 'id: docker\nlabel: Docker\nextra_field: nope\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match=str(category_path)):
        load_registry(tmp_path)


@pytest.mark.parametrize(
    ('folder', 'first_name', 'second_name', 'first_content', 'second_content', 'duplicate_id'),
    [
        (
            'categories',
            'docker.yaml',
            'docker-copy.yaml',
            'id: docker\nlabel: Docker\n',
            'id: docker\nlabel: Docker Copy\n',
            'docker',
        ),
        (
            'objects',
            'cpa.yaml',
            'cpa-copy.yaml',
            'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\n',
            'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA Copy\n',
            'cpa',
        ),
    ],
)
def test_load_registry_rejects_duplicate_ids(
    tmp_path: Path,
    folder: str,
    first_name: str,
    second_name: str,
    first_content: str,
    second_content: str,
    duplicate_id: str,
) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    if folder != 'categories':
        _write_registry_file(tmp_path, 'objects', 'base.yaml', 'id: base\ncategory: docker\ntype: docker_compose\nname: Base\n')

    _write_registry_file(tmp_path, folder, first_name, first_content)
    _write_registry_file(tmp_path, folder, second_name, second_content)

    with pytest.raises(ValueError, match=duplicate_id):
        load_registry(tmp_path)


def test_registry_service_reload_reads_updated_registry(tmp_path: Path) -> None:
    category_path = tmp_path / 'categories' / 'docker.yaml'
    object_path = tmp_path / 'objects' / 'cpa.yaml'
    _write_registry_file(tmp_path, 'categories', category_path.name, 'id: docker\nlabel: Docker\n')
    _write_registry_file(tmp_path, 'objects', object_path.name, 'id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\n')
    service = RegistryService(tmp_path)

    category_path.write_text('id: docker\nlabel: Docker Engine\n', encoding='utf-8')
    object_path.write_text('id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA Updated\n', encoding='utf-8')

    snapshot = service.reload()

    assert snapshot.categories[0].label == 'Docker Engine'
    assert snapshot.objects[0].name == 'CPA Updated'
    assert service.snapshot == snapshot
