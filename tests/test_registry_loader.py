from pathlib import Path

from app.registry.loader import load_registry


def test_load_registry_reads_categories_and_objects(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True)
    (tmp_path / 'objects').mkdir(parents=True)
    (tmp_path / 'categories' / 'docker.yaml').write_text('id: docker\nlabel: Docker\norder: 10\n')
    (tmp_path / 'objects' / 'cpa.yaml').write_text('id: cpa\ncategory: docker\ntype: docker_compose\nname: CPA\n')

    registry = load_registry(tmp_path)

    assert [category.id for category in registry.categories] == ['docker']
    assert [obj.id for obj in registry.objects] == ['cpa']
