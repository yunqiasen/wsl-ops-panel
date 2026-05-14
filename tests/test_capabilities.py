from app.models.assets import AssetSnapshot
from app.services.capabilities import enrich_asset_capabilities, get_category_actions


def test_docker_and_project_can_have_full_web_runtime_capabilities() -> None:
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
        assert 'cf_refresh' in asset.supports_actions
        assert 'notify_send' in asset.supports_actions
        assert asset.metadata['capabilities']['cf_tunnel']['applicable'] is True
        assert asset.metadata['capabilities']['wechat_notify']['applicable'] is True


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
    assert get_category_actions('system') == []
