from pathlib import Path

from app.scanners.project_scanner import scan_projects


def test_scan_projects_detects_git_node_and_python_projects(tmp_path: Path) -> None:
    root = tmp_path / 'projects'
    root.mkdir()
    app_dir = root / 'my-tool'
    app_dir.mkdir()
    (app_dir / 'package.json').write_text('{"name":"my-tool"}\n', encoding='utf-8')
    (app_dir / 'pyproject.toml').write_text('[project]\nname = "my-tool"\n', encoding='utf-8')
    (app_dir / '.git').mkdir()
    (app_dir / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    (app_dir / '.git' / 'config').write_text(
        '[remote "origin"]\n\turl = https://github.com/example/my-tool.git\n',
        encoding='utf-8',
    )

    assets = scan_projects(root=root)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.object_id == 'project__my-tool'
    assert asset.category == 'project'
    assert asset.name == 'my-tool'
    assert asset.status == 'present'
    assert asset.metadata['path'] == str(app_dir)
    assert asset.metadata['stacks'] == ['node', 'python', 'git']
    assert asset.metadata['git_remote_url'] == 'https://github.com/example/my-tool.git'
    assert asset.metadata['git_branch'] == 'main'


def test_scan_projects_skips_docker_and_hidden_backup_dirs(tmp_path: Path) -> None:
    root = tmp_path / 'projects'
    root.mkdir()
    docker_dir = root / 'docker-app'
    docker_dir.mkdir()
    (docker_dir / 'docker-compose.yml').write_text('services: {}\n', encoding='utf-8')
    hidden_dir = root / '.ops-backups'
    hidden_dir.mkdir()
    (hidden_dir / 'package.json').write_text('{}\n', encoding='utf-8')
    backup_dir = root / 'my-backup'
    backup_dir.mkdir()
    (backup_dir / 'package.json').write_text('{}\n', encoding='utf-8')

    assets = scan_projects(root=root)

    assert assets == []
