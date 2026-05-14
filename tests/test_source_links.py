from pathlib import Path

from app.scanners.docker_scanner import parse_docker_ps_lines
from app.scanners.node_scanner import parse_npm_package
from app.scanners.project_scanner import scan_projects
from app.scanners.python_scanner import parse_pip_package
from app.services.source_links import build_source_links, normalize_git_remote_url


def test_build_source_links_normalizes_docker_and_oci_urls() -> None:
    links = build_source_links(
        image_repository='searxng/searxng',
        labels={
            'org.opencontainers.image.source': 'https://github.com/searxng/searxng',
            'org.opencontainers.image.url': 'https://searxng.org',
        },
    )

    assert links == {
        'github': 'https://github.com/searxng/searxng',
        'docker': 'https://hub.docker.com/r/searxng/searxng',
        'homepage': 'https://searxng.org',
    }


def test_build_source_links_handles_ghcr_and_git_ssh() -> None:
    links = build_source_links(
        git_remote_url='git@github.com:wenfxl/openai-cpa.git',
        image_repository='ghcr.io/example/app',
    )

    assert links['github'] == 'https://github.com/wenfxl/openai-cpa'
    assert links['docker'] == 'https://github.com/example/app/pkgs/container/app'


def test_parse_docker_ps_lines_attaches_oci_labels_to_source_links() -> None:
    snapshots = parse_docker_ps_lines(
        [
            '{"ID":"1","Image":"searxng/searxng:latest",'
            '"Labels":"com.docker.compose.project=searxng,com.docker.compose.project.working_dir=/srv/searxng,'
            'com.docker.compose.service=searxng,org.opencontainers.image.source=https://github.com/searxng/searxng,'
            'org.opencontainers.image.url=https://searxng.org",'
            '"Names":"searxng","State":"running","Status":"Up 1 hour","Ports":"8080/tcp"}'
        ]
    )

    assert snapshots[0].labels['org.opencontainers.image.source'] == 'https://github.com/searxng/searxng'


def test_node_and_python_assets_include_package_source_links() -> None:
    node = parse_npm_package('@openai/codex@0.128.0')
    python = parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})

    assert node.metadata['source_links']['npm'] == 'https://www.npmjs.com/package/%40openai%2Fcodex'
    assert python.metadata['source_links']['pypi'] == 'https://pypi.org/project/fastapi/'


def test_scan_projects_reads_source_links_from_git_and_package_json(tmp_path: Path) -> None:
    project = tmp_path / 'web-tool'
    project.mkdir()
    (project / 'package.json').write_text(
        '{"repository":{"type":"git","url":"git+https://github.com/example/web-tool.git"},"homepage":"https://example.dev"}',
        encoding='utf-8',
    )
    (project / '.git').mkdir()
    (project / '.git' / 'HEAD').write_text('ref: refs/heads/main\n', encoding='utf-8')
    (project / '.git' / 'config').write_text(
        '[remote "origin"]\n\turl = git@github.com:example/web-tool.git\n',
        encoding='utf-8',
    )

    assets = scan_projects(root=tmp_path)

    assert assets[0].metadata['source_links']['github'] == 'https://github.com/example/web-tool'
    assert assets[0].metadata['source_links']['homepage'] == 'https://example.dev'
    assert normalize_git_remote_url('git+https://github.com/example/web-tool.git') == 'https://github.com/example/web-tool'
