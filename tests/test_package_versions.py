from app.services.package_versions import PackageVersionService


def test_npm_versions_parser_handles_json_array() -> None:
    service = PackageVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-1] == 'version':
            return '1.7.6\n'
        return '["1.7.4", "1.7.5", "1.7.6"]\n'

    info = service.get_node_version_info('@jackwener/opencli', runner=fake_runner)

    assert info.latest_version == '1.7.6'
    assert info.versions == ['1.7.4', '1.7.5', '1.7.6']
    assert info.source_status == 'ok'


def test_pypi_versions_wraps_failures() -> None:
    service = PackageVersionService()

    def failing_fetcher(package_name: str):
        raise RuntimeError('upstream unavailable')

    info = service.get_python_version_info('fastapi', fetcher=failing_fetcher)

    assert info.latest_version is None
    assert info.versions == []
    assert info.source_status == 'error'
    assert 'upstream unavailable' in (info.error or '')


def test_npm_versions_mark_missing_package_as_not_found() -> None:
    import subprocess

    service = PackageVersionService()

    def missing_runner(command: list[str]) -> str:
        raise subprocess.CalledProcessError(1, command, stderr='npm ERR! 404 Not Found')

    info = service.get_node_version_info('local-only-cli', runner=missing_runner)

    assert info.source_status == 'not_found'
    assert info.versions == []
    assert 'not found' in (info.error or '')


def test_pypi_versions_mark_missing_package_as_not_found() -> None:
    import httpx

    service = PackageVersionService()

    def missing_fetcher(package_name: str):
        request = httpx.Request('GET', f'https://pypi.org/pypi/{package_name}/json')
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError('not found', request=request, response=response)

    info = service.get_python_version_info('local-only-package', fetcher=missing_fetcher)

    assert info.source_status == 'not_found'
    assert info.versions == []
    assert 'not found' in (info.error or '')
