from app.models.assets import AssetSnapshot
from app.services.capabilities import enrich_asset_capabilities, get_actions_for_assets, get_category_actions


def test_docker_and_project_can_notify_web_runtime_without_cf_control() -> None:
    docker = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='cpa',
            category='docker',
            name='CPA',
            status='running',
            supports_actions=['update_latest', 'deploy_version', 'delete', 'full_delete'],
            metadata={'project_dir': '/srv/cpa', 'ports': '8317/tcp', 'web_ui': {'enabled': True}},
        )
    )
    project = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='project__demo',
            category='project',
            name='demo',
            status='present',
            metadata={'path': '/srv/demo', 'web_ui': {'enabled': True}, 'ports': '8080'},
        )
    )

    for asset in (docker, project):
        assert 'cf_refresh' not in asset.supports_actions
        assert 'notify_send' in asset.supports_actions
        assert asset.metadata['capabilities']['cf_tunnel']['applicable'] is False
        assert asset.metadata['capabilities']['wechat_notify']['applicable'] is True


def test_docker_and_project_expose_cf_only_when_cf_control_exists() -> None:
    for asset in (
        AssetSnapshot(
            object_id='cpa',
            category='docker',
            name='CPA',
            status='running',
            metadata={
                'ports': '8317/tcp',
                'capabilities': {'cf_tunnel': {'enabled': True, 'supported_actions': ['cf_create', 'cf_refresh', 'cf_disable']}},
            },
        ),
        AssetSnapshot(
            object_id='project__demo',
            category='project',
            name='demo',
            status='present',
            metadata={
                'web_ui': {'enabled': True},
                'capabilities': {'cf_tunnel': {'enabled': True, 'supported_actions': ['cf_create', 'cf_refresh', 'cf_disable']}},
            },
        ),
    ):
        enriched = enrich_asset_capabilities(asset)
        assert {'cf_create', 'cf_refresh', 'cf_disable'} <= set(enriched.supports_actions)
        assert enriched.metadata['capabilities']['cf_tunnel']['applicable'] is True


def test_node_python_and_system_do_not_get_cf_or_notify() -> None:
    node = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='node__codex',
            category='node',
            name='codex',
            status='installed',
            supports_actions=['update_latest', 'deploy_version', 'delete', 'full_delete'],
        )
    )
    python = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='python__fastapi',
            category='python',
            name='fastapi',
            status='installed',
            supports_actions=['update_latest', 'deploy_version', 'delete', 'full_delete'],
        )
    )
    system = enrich_asset_capabilities(AssetSnapshot(object_id='system__docker', category='system', name='Docker', status='present'))

    for asset in (node, python, system):
        assert 'cf_refresh' not in asset.supports_actions
        assert 'notify_send' not in asset.supports_actions
        assert asset.metadata['capabilities']['cf_tunnel']['applicable'] is False
        assert asset.metadata['capabilities']['wechat_notify']['applicable'] is False


def test_systemd_gets_runtime_and_autostart_but_not_cf_notify() -> None:
    asset = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='systemd__panel',
            category='systemd',
            name='wsl-ops-panel.service',
            status='active',
            supports_actions=['delete'],
            metadata={'unit_name': 'wsl-ops-panel.service'},
        )
    )

    assert {'start', 'stop', 'restart', 'autostart_enable', 'autostart_disable', 'delete'} <= set(asset.supports_actions)
    assert 'cf_refresh' not in asset.supports_actions
    assert 'notify_send' not in asset.supports_actions
    assert asset.metadata['capabilities']['runtime_control']['applicable'] is True
    assert asset.metadata['capabilities']['autostart']['applicable'] is True


def test_category_actions_match_simplified_scope() -> None:
    assert 'notify-send' in [item['slug'] for item in get_category_actions('docker')]
    assert 'notify-send' in [item['slug'] for item in get_category_actions('project')]
    assert 'notify-send' not in [item['slug'] for item in get_category_actions('node')]
    assert 'cf-refresh' not in [item['slug'] for item in get_category_actions('python')]
    assert 'start' in [item['slug'] for item in get_category_actions('systemd')]
    assert [item['slug'] for item in get_category_actions('system')] == ['update-latest', 'deploy-version']


def test_category_toolbar_actions_are_filtered_by_actual_assets() -> None:
    project = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='project__panel',
            category='project',
            name='wsl-ops-panel',
            status='present',
            metadata={'path': '/srv/panel', 'stacks': ['python'], 'web_ui': {'enabled': False}},
        )
    )
    docker = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='docker__web',
            category='docker',
            name='web',
            status='running',
            metadata={
                'ports': '8080/tcp',
                'capabilities': {'cf_tunnel': {'enabled': True, 'supported_actions': ['cf_create', 'cf_refresh', 'cf_disable']}},
            },
        )
    )

    project_slugs = [item['slug'] for item in get_actions_for_assets('project', [project])]
    docker_slugs = [item['slug'] for item in get_actions_for_assets('docker', [docker])]

    assert project_slugs == ['update-latest', 'deploy-version', 'delete', 'full-delete']
    assert 'cf-refresh' in docker_slugs
    assert 'notify-send' in docker_slugs


def test_system_assets_expose_safe_update_metadata() -> None:
    cloudflared = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='system__cloudflared',
            category='system',
            name='cloudflared',
            status='available',
            supports_actions=['update_latest', 'deploy_version'],
            metadata={'update_supported': True},
        )
    )
    python = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='system__python',
            category='system',
            name='python',
            status='available',
            supports_actions=[],
            metadata={'update_supported': False, 'update_note': '不建议面板更新系统 Python'},
        )
    )

    assert cloudflared.metadata['capabilities']['versioning']['enabled'] is True
    assert 'update_latest' in cloudflared.supports_actions
    assert python.metadata['capabilities']['versioning']['enabled'] is False
    assert 'update_latest' not in python.supports_actions


def test_source_only_project_does_not_expose_runtime_actions_without_start_entrypoint() -> None:
    asset = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='project__webclone',
            category='project',
            name='webclone',
            status='present',
            metadata={
                'path': '/srv/webclone',
                'package_scripts': {'test': 'node --test', 'lint': 'eslint .'},
                'start_command': None,
                'web_ui': {'enabled': False},
            },
        )
    )

    assert 'start' not in asset.supports_actions
    assert 'stop' not in asset.supports_actions
    assert 'restart' not in asset.supports_actions
    assert asset.metadata['capabilities']['runtime_control']['enabled'] is False
