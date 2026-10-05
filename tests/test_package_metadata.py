from app.services.package_metadata import PackageMetadataService


def test_node_package_metadata_normalizes_repository() -> None:
    service = PackageMetadataService()
    info = service.get_node_metadata(
        '@openai/codex',
        fetcher=lambda name: {
            'description': 'Codex CLI',
            'homepage': 'https://github.com/openai/codex',
            'repository': {'type': 'git', 'url': 'git+https://github.com/openai/codex.git'},
            'license': 'Apache-2.0',
            'keywords': ['ai', 'cli'],
        },
    )

    assert info.status == 'ok'
    assert info.description == 'Codex CLI'
    assert info.repository == 'https://github.com/openai/codex'
    assert info.package_url == 'https://www.npmjs.com/package/@openai%2Fcodex'
    assert info.keywords == ['ai', 'cli']


def test_python_package_metadata_uses_pypi_info() -> None:
    service = PackageMetadataService()
    info = service.get_python_metadata(
        'fastapi',
        fetcher=lambda name: {
            'info': {
                'summary': 'FastAPI framework',
                'home_page': 'https://fastapi.tiangolo.com/',
                'project_urls': {'Source': 'https://github.com/fastapi/fastapi'},
                'keywords': 'api web async',
                'license': 'MIT',
            }
        },
    )

    assert info.status == 'ok'
    assert info.description == 'FastAPI framework'
    assert info.homepage == 'https://fastapi.tiangolo.com/'
    assert info.repository == 'https://github.com/fastapi/fastapi'
    assert info.package_url == 'https://pypi.org/project/fastapi/'


def test_package_metadata_errors_are_returned_not_raised() -> None:
    service = PackageMetadataService()
    info = service.get_python_metadata('missing', fetcher=lambda name: (_ for _ in ()).throw(RuntimeError('boom')))

    assert info.status == 'error'
    assert info.error == 'boom'
