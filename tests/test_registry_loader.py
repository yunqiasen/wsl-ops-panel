from pathlib import Path

import pytest

from app.registry.loader import load_registry
from app.registry.service import RegistryService


def _write_registry_file(root: Path, folder: str, name: str, content: str) -> None:
    directory = root / folder
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding='utf-8')


def _docker_object_yaml(
    *,
    object_id: str = 'cpa',
    category: str = 'docker',
    name: str = 'CPA',
    config: str = "  project_dir: /srv/cpa\n  compose_file: docker-compose.yml\n",
) -> str:
    return (
        f'id: {object_id}\n'
        f'category: {category}\n'
        'type: docker_compose\n'
        f'name: {name}\n'
        'config:\n'
        f'{config}'
    )


def _systemd_object_yaml(
    *,
    object_id: str = 'panel',
    category: str = 'systemd',
    name: str = 'Panel',
    config: str = "  unit_name: panel.service\n  working_dir: /srv/panel\n",
) -> str:
    return (
        f'id: {object_id}\n'
        f'category: {category}\n'
        'type: systemd_unit\n'
        f'name: {name}\n'
        'config:\n'
        f'{config}'
    )


def test_load_registry_reads_categories_and_objects(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())

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
    _write_registry_file(tmp_path, 'objects', object_path.name, _docker_object_yaml(category='missing'))

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
            _docker_object_yaml(),
            _docker_object_yaml(name='CPA Copy'),
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
        _write_registry_file(tmp_path, 'objects', 'base.yaml', _docker_object_yaml(object_id='base', name='Base'))

    _write_registry_file(tmp_path, folder, first_name, first_content)
    _write_registry_file(tmp_path, folder, second_name, second_content)

    with pytest.raises(ValueError, match=duplicate_id):
        load_registry(tmp_path)


@pytest.mark.parametrize(
    ('category_id', 'type_name', 'object_content', 'error_match'),
    [
        (
            'docker',
            'docker_compose',
            '''id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\nconfig:\n  compose_file: docker-compose.yml\n''',
            'project_dir',
        ),
        (
            'docker',
            'docker_compose',
            '''id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\nconfig:\n  project_dir: /srv/cpa\n  compose_file: 123\n''',
            'compose_file',
        ),
        (
            'systemd',
            'systemd_unit',
            '''id: panel\ncategory: systemd\ntype: systemd_unit\nname: Panel\nconfig:\n  working_dir: /srv/panel\n''',
            'unit_name',
        ),
        (
            'systemd',
            'systemd_unit',
            '''id: panel\ncategory: systemd\ntype: systemd_unit\nname: Panel\nconfig:\n  unit_name: panel.service\n  working_dir: false\n''',
            'working_dir',
        ),
    ],
)
def test_load_registry_rejects_invalid_known_object_config(
    tmp_path: Path,
    category_id: str,
    type_name: str,
    object_content: str,
    error_match: str,
) -> None:
    _write_registry_file(tmp_path, 'categories', f'{category_id}.yaml', f'id: {category_id}\nlabel: {type_name}\n')
    object_path = tmp_path / 'objects' / f'{type_name}.yaml'
    _write_registry_file(tmp_path, 'objects', object_path.name, object_content)

    with pytest.raises(ValueError, match=error_match):
        load_registry(tmp_path)

    with pytest.raises(ValueError, match=str(object_path)):
        load_registry(tmp_path)


def test_registry_service_snapshot_returns_safe_copy(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    service = RegistryService(tmp_path)

    snapshot = service.snapshot
    snapshot.categories[0].label = 'Mutated'
    snapshot.objects[0].name = 'Mutated Object'
    snapshot.objects.append(snapshot.objects[0].model_copy(deep=True))

    fresh_snapshot = service.snapshot

    assert fresh_snapshot.categories[0].label == 'Docker'
    assert fresh_snapshot.objects[0].name == 'CPA'
    assert len(fresh_snapshot.objects) == 1


def test_registry_service_reload_reads_updated_registry(tmp_path: Path) -> None:
    category_path = tmp_path / 'categories' / 'docker.yaml'
    object_path = tmp_path / 'objects' / 'cpa.yaml'
    _write_registry_file(tmp_path, 'categories', category_path.name, 'id: docker\nlabel: Docker\n')
    _write_registry_file(tmp_path, 'objects', object_path.name, _docker_object_yaml())
    service = RegistryService(tmp_path)

    category_path.write_text('id: docker\nlabel: Docker Engine\n', encoding='utf-8')
    object_path.write_text(_docker_object_yaml(name='CPA Updated'), encoding='utf-8')

    snapshot = service.reload()

    assert snapshot.categories[0].label == 'Docker Engine'
    assert snapshot.objects[0].name == 'CPA Updated'
    assert service.snapshot == snapshot


def test_load_registry_accepts_extended_docker_config(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'openai-cpa.yaml',
        _docker_object_yaml(
            object_id='openai_cpa',
            name='openai-cpa',
            config='  project_dir: /srv/openai-cpa\n'
            '  compose_file: docker-compose.yml\n'
            '  primary_container: wenfxl_codex_manager\n'
            '  compose_service: codex-web\n'
            '  lifecycle_strategy: compose_local_build_git_tag\n'
            '  version_source: git_tags\n'
            '  recipe_id: openai-cpa\n'
            '  managed_services:\n'
            '    - codex-web\n'
            '  ignored_services:\n'
            '    - watchtower\n'
            '  healthcheck_url: http://127.0.0.1:8128\n',
        ),
    )

    registry = load_registry(tmp_path)

    assert registry.objects[0].config['recipe_id'] == 'openai-cpa'
    assert registry.objects[0].config['managed_services'] == ['codex-web']
