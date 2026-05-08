from app.models.assets import DockerContainerSnapshot
from app.services.docker_versions import DockerVersionService


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


def test_registry_tag_version_info_uses_payload_order_for_versions_and_latest() -> None:
    service = DockerVersionService()

    def fake_fetcher(image_repository: str) -> dict:
        assert image_repository == 'library/nginx'
        return {
            'results': [
                {'name': '1.27.0'},
                {'name': 'mainline'},
                {'name': '1.26.3'},
            ]
        }

    info = service.get_registry_tag_version_info(
        'library/nginx',
        current_version='1.26.3',
        fetcher=fake_fetcher,
    )

    assert info.current_version == '1.26.3'
    assert info.latest_version == '1.27.0'
    assert info.versions == ['1.27.0', 'mainline', '1.26.3']
    assert info.version_source == 'docker_hub_tags'
    assert info.source_status == 'ok'
