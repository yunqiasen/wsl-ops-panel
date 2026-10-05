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


def test_scan_projects_detects_web_ui_and_runtime_hints(tmp_path: Path) -> None:
    project = tmp_path / 'web-project'
    project.mkdir()
    (project / 'package.json').write_text(
        '{"scripts":{"start":"vite --host 0.0.0.0 --port 5173"},"dependencies":{"vite":"latest"}}',
        encoding='utf-8',
    )
    (project / 'logs').mkdir()
    (project / 'logs' / 'cftunnel-domain.txt').write_text('https://demo.trycloudflare.com', encoding='utf-8')
    (project / 'scripts').mkdir()
    (project / 'scripts' / 'cftunnel-start.sh').write_text('#!/usr/bin/env bash\n', encoding='utf-8')

    assets = scan_projects(root=tmp_path)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.metadata['web_ui']['enabled'] is True
    assert asset.metadata['web_ui']['port'] == '5173'
    assert asset.metadata['start_command'] == 'vite --host 0.0.0.0 --port 5173'
    assert asset.metadata['capabilities']['cf_tunnel']['enabled'] is True
    assert asset.metadata['capabilities']['cf_tunnel']['current_url'] == 'https://demo.trycloudflare.com'


def test_scan_projects_detects_systemd_runtime_unit_and_web_port(tmp_path: Path) -> None:
    project = tmp_path / 'regmail-2api'
    project.mkdir()
    (project / 'requirements.txt').write_text('fastapi\n', encoding='utf-8')
    router = project / 'ui' / 'frontend' / 'src' / 'router'
    router.mkdir(parents=True)
    (router / 'index.ts').write_text("const routes = [{ path: '/', redirect: '/email' }, { path: '/email' }]\n", encoding='utf-8')
    unit_root = tmp_path / 'units'
    unit_root.mkdir()
    (unit_root / 'regmail-ui.service').write_text(
        '\n'.join(
            [
                '[Unit]',
                'Description=Regmail UI',
                '[Service]',
                f'WorkingDirectory={project}',
                'ExecStart=/python -m uvicorn ui.backend.main:app --host 0.0.0.0 --port 45345',
                '[Install]',
                'WantedBy=multi-user.target',
            ]
        ),
        encoding='utf-8',
    )

    assets = scan_projects(
        root=tmp_path,
        systemd_unit_roots=[unit_root],
        command_runner=lambda command, timeout=1.0: 'active\n' if command == ['systemctl', 'is-active', 'regmail-ui.service'] else '',
    )

    assert len(assets) == 1
    asset = assets[0]
    assert asset.status == 'active'
    assert asset.metadata['service_unit'] == 'regmail-ui.service'
    assert asset.metadata['web_ui']['enabled'] is True
    assert asset.metadata['web_ui']['port'] == '45345'
    assert asset.metadata['web_ui']['url_path'] == '/email'
    assert asset.metadata['ports'] == '45345/tcp'
    assert asset.metadata['capabilities']['runtime_control']['enabled'] is True
    assert asset.metadata['capabilities']['autostart']['enabled'] is True


def test_scan_projects_can_include_nested_ai_project(tmp_path: Path) -> None:
    root = tmp_path / 'root'
    ai = root / 'AI'
    panel = ai / 'wsl-ops-panel'
    panel.mkdir(parents=True)
    (panel / 'pyproject.toml').write_text('[project]\nname = "wsl-ops-panel"\n', encoding='utf-8')
    (panel / '.git').mkdir()
    (panel / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')

    assets = scan_projects(roots=[root, ai])

    assert any(asset.object_id == 'project__wsl-ops-panel' for asset in assets)

def test_scan_projects_reads_package_description(tmp_path: Path) -> None:
    project = tmp_path / 'node-tool'
    project.mkdir()
    (project / 'package.json').write_text(
        '{"name":"node-tool","description":"Node 工具项目简介"}\n',
        encoding='utf-8',
    )

    assets = scan_projects(root=tmp_path)

    assert len(assets) == 1
    assert assets[0].metadata['description'] == 'Node 工具项目简介'


def test_scan_projects_can_scan_an_explicit_project_root_itself(tmp_path: Path) -> None:
    project = tmp_path / 'webclone'
    project.mkdir()
    (project / 'package.json').write_text(
        '{"name":"webclone","description":"网站离线归档 CLI"}\n',
        encoding='utf-8',
    )

    assets = scan_projects(roots=[project])

    assert [asset.object_id for asset in assets] == ['project__webclone']
    assert assets[0].metadata['path'] == str(project)
    assert assets[0].metadata['description'] == '网站离线归档 CLI'


def test_scan_projects_detects_related_user_systemd_units(tmp_path: Path) -> None:
    project = tmp_path / 'oai-cpa-tools'
    project.mkdir()
    (project / 'server.py').write_text('print("dashboard")\n', encoding='utf-8')
    unit_root = tmp_path / '.config' / 'systemd' / 'user'
    unit_root.mkdir(parents=True)
    (unit_root / 'oai-cpa-tools.service').write_text(
        '[Service]\n'
        f'WorkingDirectory={tmp_path}\n'
        f'ExecStart=/usr/bin/python3 {project}/server.py --port 8765\n',
        encoding='utf-8',
    )
    (unit_root / 'oai-cpa-domain-monitor.service').write_text(
        '[Service]\n'
        f'WorkingDirectory={tmp_path}\n'
        f'ExecStart=/usr/bin/python3 {project}/monitor.py\n',
        encoding='utf-8',
    )
    commands: list[list[str]] = []

    def runner(command: list[str], timeout: float = 1.0) -> str:
        commands.append(command)
        return 'active\n'

    assets = scan_projects(
        roots=[project],
        systemd_unit_roots=[unit_root],
        command_runner=runner,
    )

    assert [asset.object_id for asset in assets] == ['project__oai-cpa-tools']
    asset = assets[0]
    assert asset.status == 'active'
    assert asset.metadata['service_unit'] == 'oai-cpa-tools.service'
    assert asset.metadata['service_units'] == [
        'oai-cpa-domain-monitor.service',
        'oai-cpa-tools.service',
    ]
    assert asset.metadata['service_scope'] == 'user'
    assert asset.metadata['web_ui']['port'] == '8765'
    assert ['systemctl', '--user', 'is-active', 'oai-cpa-tools.service'] in commands


def test_parent_project_keeps_its_direct_unit_and_does_not_absorb_nested_project_units(tmp_path: Path) -> None:
    parent = tmp_path / 'regmail-2api'
    child = parent / '资源' / 'oai-cpa-tools'
    child.mkdir(parents=True)
    (parent / 'requirements.txt').write_text('fastapi\n', encoding='utf-8')
    (child / 'server.py').write_text('print("tools")\n', encoding='utf-8')
    system_root = tmp_path / 'system'
    system_root.mkdir()
    (system_root / 'regmail-ui.service').write_text(
        '[Service]\n'
        f'WorkingDirectory={parent}\n'
        f'ExecStart=/usr/bin/python3 -m uvicorn app:main --port 45345\n',
        encoding='utf-8',
    )
    user_root = tmp_path / '.config' / 'systemd' / 'user'
    user_root.mkdir(parents=True)
    (user_root / 'oai-cpa-tools.service').write_text(
        '[Service]\n'
        f'WorkingDirectory={parent / "资源"}\n'
        f'ExecStart=/usr/bin/python3 {child}/server.py --port 8765\n',
        encoding='utf-8',
    )

    assets = scan_projects(
        roots=[parent],
        systemd_unit_roots=[system_root, user_root],
        command_runner=lambda command, timeout=1.0: 'active\n',
    )

    assert [asset.object_id for asset in assets] == ['project__regmail-2api']
    assert assets[0].metadata['service_unit'] == 'regmail-ui.service'
    assert assets[0].metadata['service_units'] == ['regmail-ui.service']
    assert assets[0].metadata['service_scope'] == 'system'
    assert assets[0].metadata['web_ui']['port'] == '45345'


def test_scan_projects_treats_inactive_unit_with_live_listener_as_active_runtime(tmp_path: Path) -> None:
    project = tmp_path / 'regmail-2api'
    project.mkdir()
    (project / 'requirements.txt').write_text('fastapi\n', encoding='utf-8')
    unit_root = tmp_path / 'system'
    unit_root.mkdir()
    (unit_root / 'regmail-ui.service').write_text(
        '[Service]\n'
        f'WorkingDirectory={project}\n'
        'ExecStart=/usr/bin/python3 -m uvicorn app:main --host 0.0.0.0 --port 45345\n',
        encoding='utf-8',
    )

    def runner(command: list[str], timeout: float = 1.0) -> str:
        if command[:2] == ['systemctl', 'is-active']:
            return 'inactive\n'
        if command == ['ss', '-ltn']:
            return 'LISTEN 0 2048 0.0.0.0:45345 0.0.0.0:*\n'
        return ''

    assets = scan_projects(
        roots=[project],
        systemd_unit_roots=[unit_root],
        command_runner=runner,
    )

    assert assets[0].status == 'active'
    assert assets[0].metadata['status_source'] == 'listening_port'
