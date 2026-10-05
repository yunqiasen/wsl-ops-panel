from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.scanners.host_process_scanner import parse_listening_socket
from app.scanners.node_scanner import parse_npm_package
from app.scanners.python_scanner import parse_pip_package
from app.scanners.system_scanner import parse_version_output


def _write_category(root: Path, filename: str, content: str) -> None:
    directory = root / 'categories'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(content, encoding='utf-8')


def _write_empty_objects_dir(root: Path) -> None:
    (root / 'objects').mkdir(parents=True, exist_ok=True)


def test_parse_npm_package() -> None:
    asset = parse_npm_package('@openai/codex@0.128.0')

    assert asset.name == '@openai/codex'
    assert asset.current_version == '0.128.0'
    assert asset.category == 'node'


def test_node_asset_ids_preserve_package_uniqueness() -> None:
    first = parse_npm_package('@foo/bar@1.0.0')
    second = parse_npm_package('foo-bar@1.0.0')

    assert first.object_id != second.object_id


def test_parse_pip_package() -> None:
    asset = parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})

    assert asset.name == 'fastapi'
    assert asset.current_version == '0.115.0'
    assert asset.category == 'python'


def test_parse_listening_socket() -> None:
    asset = parse_listening_socket(
        'LISTEN 0 128 0.0.0.0:45345 0.0.0.0:* users:(("python3",pid=1234,fd=7))'
    )

    assert asset.name == 'python3'
    assert asset.status == 'listening'
    assert asset.metadata['port'] == '45345'
    assert asset.metadata['pid'] == 1234


def test_host_asset_ids_include_binding_details() -> None:
    first = parse_listening_socket('LISTEN 0 128 0.0.0.0:53 0.0.0.0:* users:(("dnsd",pid=10,fd=7))')
    second = parse_listening_socket('LISTEN 0 128 [::]:53 [::]:* users:(("dnsd",pid=10,fd=8))')

    assert first.object_id != second.object_id


def test_parse_system_version_output() -> None:
    asset = parse_version_output('docker', 'Docker version 27.5.1, build abcdef')

    assert asset.name == 'docker'
    assert asset.current_version == '27.5.1'
    assert asset.category == 'system'


def test_system_scanner_exposes_apt_as_safe_check_asset() -> None:
    asset = parse_version_output('apt_packages', '12 packages can be upgraded. Run apt list --upgradable to see them.')

    assert asset.name == 'APT / 系统软件包'
    assert asset.category == 'system'
    assert asset.current_version == '12 upgradable'
    assert asset.supports_actions == ['update_latest']
    assert asset.metadata['update_command'] == 'sudo apt update && apt list --upgradable'
    assert asset.metadata['upgrade_command'] == 'sudo apt upgrade -y'
    assert asset.metadata['update_note'] == '面板内默认只刷新软件包索引并列出可升级项，不直接执行 apt upgrade -y。'


def test_readonly_category_pages_and_detail_render_assets(tmp_path: Path) -> None:
    _write_category(tmp_path, 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_category(tmp_path, 'systemd.yaml', 'id: systemd\nlabel: systemd\norder: 20\nenabled: true\n')
    _write_category(tmp_path, 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    _write_category(tmp_path, 'python.yaml', 'id: python\nlabel: Python\norder: 40\nenabled: true\n')
    _write_category(tmp_path, 'host.yaml', 'id: host\nlabel: 宿主机进程\norder: 80\nenabled: true\n')
    _write_category(tmp_path, 'system.yaml', 'id: system\nlabel: 系统基础设施\norder: 90\nenabled: true\n')
    _write_empty_objects_dir(tmp_path)

    node_assets = [parse_npm_package('@openai/codex@0.128.0')]
    python_assets = [parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})]
    host_assets = [
        parse_listening_socket('LISTEN 0 128 0.0.0.0:45345 0.0.0.0:* users:(("python3",pid=1234,fd=7))')
    ]
    system_assets = [parse_version_output('docker', 'Docker version 27.5.1, build abcdef')]

    client = TestClient(
        create_app(
            config_root=tmp_path,
            node_scanner=lambda: node_assets,
            python_scanner=lambda: python_assets,
            host_process_scanner=lambda: host_assets,
            system_infra_scanner=lambda: system_assets,
        )
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    node_response = client.get('/categories/node')
    assert node_response.status_code == 200
    assert '@openai/codex' in node_response.text
    assert 'Node' in node_response.text

    python_response = client.get('/categories/python')
    assert python_response.status_code == 200
    assert 'fastapi' in python_response.text

    host_response = client.get('/categories/host')
    assert host_response.status_code == 200
    assert '45345' in host_response.text
    assert 'python3' in host_response.text

    system_response = client.get('/categories/system')
    assert system_response.status_code == 200
    assert 'Docker version 27.5.1, build abcdef' in system_response.text

    detail_response = client.get(f"/assets/{node_assets[0].object_id}")
    assert detail_response.status_code == 200
    assert '@openai/codex' in detail_response.text
    assert '0.128.0' in detail_response.text


def test_node_and_python_pages_show_policy_badges_and_block_reasons(tmp_path: Path) -> None:
    categories = tmp_path / 'categories'
    categories.mkdir(parents=True, exist_ok=True)
    (categories / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (categories / 'python.yaml').write_text('id: python\nlabel: Python\norder: 40\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: "@openai/codex"\n    managed_by: agent_cli\n    protected: true\n    blocked_reason: 保留给 agent cli\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text(
        'packages:\n  - name: fastapi\n    managed_by: python\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )

    client = TestClient(
        create_app(
            config_root=tmp_path,
            node_scanner=lambda: [parse_npm_package('@openai/codex@0.128.0')],
            python_scanner=lambda: [parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})],
        )
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    node_page = client.get('/categories/node')
    assert '保留给 agent cli' in node_page.text
    assert '只读' in node_page.text

    python_page = client.get('/categories/python')
    assert '可操作' in python_page.text
    assert 'fastapi' in python_page.text


def test_host_socket_enrichment_identifies_known_port_and_docker_owner() -> None:
    asset = parse_listening_socket('LISTEN 0 128 0.0.0.0:8317 0.0.0.0:* users:(("docker-proxy",pid=1234,fd=7))')

    assert asset.metadata['service_hint'] == 'CPA / CLIProxyAPI'
    assert asset.metadata['port_action'] == 'docker'
    assert asset.metadata['purpose'] == 'CPA API / CLIProxyAPI 网页与 API 入口'
    assert asset.metadata['target_asset_id'] == 'cpa'
    assert asset.metadata['target_container_name'] == 'cli-proxy-api'
