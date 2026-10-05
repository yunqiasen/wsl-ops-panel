from app.models.assets import DockerContainerSnapshot
from app.services.docker_versions import DockerVersionService, REMOTE_TAG_REF_NAMESPACE


def test_git_tag_version_info_prefers_semver_and_current_tag() -> None:
    service = DockerVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-3:] == ['tag', '--points-at', 'HEAD']:
            return 'v14.2.6\n'
        if command[-2:] == ['tag', '--list']:
            return 'v14.2.4\nv14.2.6\nv14.2.5\n'
        return ''

    info = service.get_git_tag_version_info('/srv/openai-cpa', runner=fake_runner, fetch=False)

    assert info.current_version == 'v14.2.6'
    assert info.latest_version == 'v14.2.6'
    assert info.versions[:3] == ['v14.2.6', 'v14.2.5', 'v14.2.4']
    assert info.source_status == 'ok'
    assert info.version_source == 'git_tags'


def test_git_tag_version_info_uses_local_tags_when_fetch_fails() -> None:
    service = DockerVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-4:] == ['fetch', '--tags', '--force', 'origin']:
            raise RuntimeError('network unavailable')
        if command[-3:] == ['tag', '--points-at', 'HEAD']:
            return 'v14.2.5\n'
        if command[-2:] == ['tag', '--list']:
            return 'v14.2.4\nv14.2.6\nv14.2.5\n'
        return ''

    info = service.get_git_tag_version_info('/srv/openai-cpa', runner=fake_runner, fetch=True)

    assert info.current_version == 'v14.2.5'
    assert info.latest_version == 'v14.2.6'
    assert info.versions == ['v14.2.6', 'v14.2.5', 'v14.2.4']
    assert info.source_status == 'ok'
    assert info.version_source == 'git_tags'


def test_git_tag_version_info_can_read_remote_tags_without_fetching() -> None:
    service = DockerVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-3:] == ['tag', '--points-at', 'HEAD']:
            return ''
        if command[-7:] == ['-c', 'http.version=HTTP/1.1', 'ls-remote', '--tags', '--refs', 'origin', 'v*']:
            return (
                'aaa\trefs/tags/v15.0.2\n'
                'bbb\trefs/tags/v15.0.3\n'
            )
        if command[-2:] == ['tag', '--list']:
            return 'v15.0.2\n'
        raise AssertionError(f'unexpected command: {command}')

    info = service.get_git_tag_version_info('/srv/openai-cpa', runner=fake_runner, remote=True)

    assert info.latest_version == 'v15.0.3'
    assert info.versions == ['v15.0.3', 'v15.0.2']
    assert info.source_status == 'ok'


def test_git_tag_version_info_ignores_internal_remote_tag_namespace() -> None:
    service = DockerVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-3:] == ['tag', '--points-at', 'HEAD']:
            return ''
        if command[-7:] == ['-c', 'http.version=HTTP/1.1', 'ls-remote', '--tags', '--refs', 'origin', 'v*']:
            return ''
        if command[-2:] == ['tag', '--list']:
            return (
                'v15.0.2\n'
                f'{REMOTE_TAG_REF_NAMESPACE}/v15.0.3\n'
            )
        return ''

    info = service.get_git_tag_version_info('/srv/openai-cpa', runner=fake_runner, remote=True)

    assert info.versions == ['v15.0.2']


def test_runtime_version_info_reads_oci_labels() -> None:
    service = DockerVersionService()
    container = DockerContainerSnapshot(
        id='1',
        name='wenfxl_codex_manager',
        image='local/wenfxl-codex-manager:v14.2.6-overlay-uifix',
        image_tag='v14.2.6-overlay-uifix',
        status='Up 10 minutes',
        ports='8128/tcp',
        labels={
            'org.opencontainers.image.version': '14.2.4',
            'org.opencontainers.image.revision': 'ece08961',
        },
    )

    runtime = service.build_runtime_version_info(container)

    assert runtime.image_tag == 'v14.2.6-overlay-uifix'
    assert runtime.oci_version == '14.2.4'
    assert runtime.oci_revision == 'ece08961'


def test_registry_tag_version_info_prefers_highest_semver_for_latest() -> None:
    service = DockerVersionService()

    def fake_fetcher(image_repository: str) -> dict:
        assert image_repository == 'library/nginx'
        return {
            'results': [
                {'name': 'mainline'},
                {'name': '1.26.3'},
                {'name': '1.26.3'},
                {'name': '1.27.0'},
            ]
        }

    info = service.get_registry_tag_version_info(
        'library/nginx',
        current_version='1.26.3',
        fetcher=fake_fetcher,
    )

    assert info.current_version == '1.26.3'
    assert info.latest_version == '1.27.0'
    assert info.versions == ['mainline', '1.26.3', '1.26.3', '1.27.0']
    assert info.version_source == 'registry_tags'
    assert info.source_status == 'ok'


def test_registry_tag_version_info_falls_back_to_first_tag_without_semver() -> None:
    service = DockerVersionService()

    def fake_fetcher(image_repository: str) -> dict:
        assert image_repository == 'library/nginx'
        return {
            'results': [
                {'name': 'latest'},
                {'name': 'mainline'},
            ]
        }

    info = service.get_registry_tag_version_info(
        'library/nginx',
        current_version='latest',
        fetcher=fake_fetcher,
    )

    assert info.current_version == 'latest'
    assert info.latest_version == 'latest'
    assert info.versions == ['latest', 'mainline']
    assert info.version_source == 'registry_tags'
    assert info.source_status == 'ok'


def test_registry_tag_version_info_normalizes_official_docker_hub_images() -> None:
    service = DockerVersionService()
    calls: list[str] = []

    def fake_fetcher(image_repository: str) -> dict:
        calls.append(image_repository)
        return {'results': [{'name': 'latest'}, {'name': '1.27.0'}]}

    info = service.get_registry_tag_version_info('nginx', current_version='alpine', fetcher=fake_fetcher)

    assert calls == ['library/nginx']
    assert info.current_version == 'alpine'
    assert info.latest_version == '1.27.0'
    assert info.versions == ['latest', '1.27.0']
    assert info.source_status == 'ok'


def test_registry_tag_version_info_marks_local_images_without_remote_failure() -> None:
    service = DockerVersionService()

    def fail_fetcher(_image_repository: str) -> dict:
        raise AssertionError('local images must not hit remote registries')

    info = service.get_registry_tag_version_info('local/openai-cpa-patched', current_version='latest', fetcher=fail_fetcher)

    assert info.current_version == 'latest'
    assert info.latest_version == 'latest'
    assert info.versions == ['latest']
    assert info.source_status == 'local'
    assert info.version_source == 'local_image'
    assert info.error is None


def test_registry_tag_version_info_fetches_ghcr_tags_from_registry_api() -> None:
    service = DockerVersionService()
    calls: list[str] = []

    def fake_fetcher(image_repository: str) -> dict:
        calls.append(image_repository)
        return {'results': [{'name': 'latest'}, {'name': '1.0.0'}, {'name': '1.2.0'}]}

    info = service.get_registry_tag_version_info('ghcr.io/example/app', current_version='latest', fetcher=fake_fetcher)

    assert calls == ['example/app']
    assert info.latest_version == '1.2.0'
    assert info.versions == ['latest', '1.0.0', '1.2.0']
    assert info.source_status == 'ok'
    assert info.version_source == 'ghcr_tags'



def test_remote_git_tags_have_timeout_and_fallback_to_local_tags() -> None:
    service = DockerVersionService()
    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> str:
        commands.append(command)
        if command[-3:] == ['tag', '--points-at', 'HEAD']:
            return ''
        if command[-7:] == ['-c', 'http.version=HTTP/1.1', 'ls-remote', '--tags', '--refs', 'origin', 'v*']:
            raise RuntimeError('remote timeout')
        if command[-2:] == ['tag', '--list']:
            return 'v15.0.2\n'
        raise AssertionError(f'unexpected command: {command}')

    info = service.get_git_tag_version_info('/srv/openai-cpa', runner=fake_runner, remote=True)

    remote_command = next(command for command in commands if 'ls-remote' in command)
    assert remote_command[:2] == ['timeout', '20']
    assert remote_command[2] == 'git'
    assert info.latest_version == 'v15.0.2'
    assert info.versions == ['v15.0.2']
    assert info.source_status == 'ok'


def _http_status_error(status_code: int):
    import httpx

    request = httpx.Request('GET', 'https://example.test/tags')
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f'status {status_code}', request=request, response=response)


def test_registry_tag_version_info_treats_404_as_local_or_unpublished_image() -> None:
    service = DockerVersionService()

    def missing_fetcher(_image_repository: str) -> dict:
        raise _http_status_error(404)

    info = service.get_registry_tag_version_info(
        'xinghai-image-studio-ui-web',
        current_version='latest',
        fetcher=missing_fetcher,
    )

    assert info.current_version == 'latest'
    assert info.latest_version is None
    assert info.versions == ['latest']
    assert info.source_status == 'local'
    assert info.version_source == 'local_image'
    assert 'not published' in (info.error or '')


def test_registry_tag_version_info_marks_auth_required_without_raw_http_error() -> None:
    service = DockerVersionService()

    def auth_fetcher(_image_repository: str) -> dict:
        raise _http_status_error(401)

    info = service.get_registry_tag_version_info(
        'ghcr.io/lolollipop/team-manage-refresh',
        current_version='latest',
        fetcher=auth_fetcher,
    )

    assert info.current_version == 'latest'
    assert info.latest_version is None
    assert info.versions == ['latest']
    assert info.source_status == 'auth_required'
    assert info.version_source == 'ghcr_tags'
    assert 'requires authentication' in (info.error or '')
    assert '401' not in (info.error or '')


def test_remote_git_version_info_reads_github_tags_with_timeout() -> None:
    service = DockerVersionService()
    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> str:
        commands.append(command)
        assert command[:2] == ['timeout', '20']
        assert command[2:6] == ['git', '-c', 'http.version=HTTP/1.1', 'ls-remote']
        assert command[-1] == 'https://github.com/loLollipop/team-manage-refresh'
        return (
            'aaa\trefs/tags/v0.2.4\n'
            'bbb\trefs/tags/v0.2.5\n'
            'ccc\trefs/tags/not-a-version\n'
        )

    info = service.get_remote_git_version_info(
        'https://github.com/loLollipop/team-manage-refresh',
        current_version='0.2.5',
        runner=fake_runner,
    )

    assert commands
    assert info.current_version == '0.2.5'
    assert info.latest_version == 'v0.2.5'
    assert info.versions == ['v0.2.5', 'v0.2.4']
    assert info.source_status == 'ok'
    assert info.version_source == 'github_tags'


def test_registry_auth_failure_falls_back_to_github_tags_when_source_url_exists() -> None:
    service = DockerVersionService()

    def auth_fetcher(_image_repository: str) -> dict:
        raise _http_status_error(401)

    def fake_runner(_command: list[str]) -> str:
        return 'aaa\trefs/tags/v0.2.5\n'

    info = service.get_registry_tag_version_info(
        'ghcr.io/lolollipop/team-manage-refresh',
        current_version='0.2.5',
        source_url='https://github.com/loLollipop/team-manage-refresh',
        fetcher=auth_fetcher,
        runner=fake_runner,
    )

    assert info.latest_version == 'v0.2.5'
    assert info.versions == ['v0.2.5']
    assert info.source_status == 'ok'
    assert info.version_source == 'github_tags'
