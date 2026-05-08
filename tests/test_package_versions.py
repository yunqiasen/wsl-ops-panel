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
