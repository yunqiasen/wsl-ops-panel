from app.adapters.project_adapter import ProjectAdapter


def test_project_adapter_plans_git_pull_update_and_tag_deploy() -> None:
    adapter = ProjectAdapter(project_dir='/srv/wsl-ops-panel', current_version='main')

    update = adapter.plan_action('update_latest')
    deploy = adapter.plan_action('deploy_version', version='v1.2.3')

    assert update.commands == [['git', '-C', '/srv/wsl-ops-panel', 'pull', '--ff-only']]
    assert deploy.commands == [['git', '-C', '/srv/wsl-ops-panel', 'fetch', '--tags'], ['git', '-C', '/srv/wsl-ops-panel', 'checkout', 'v1.2.3']]


def test_project_adapter_plans_safe_delete_and_full_delete() -> None:
    adapter = ProjectAdapter(project_dir='/srv/wsl-ops-panel', current_version='main')

    delete = adapter.plan_action('delete')
    full_delete = adapter.plan_action('full_delete')

    assert delete.commands == [['true']]
    assert delete.preview_paths == ['/srv/wsl-ops-panel']
    assert full_delete.commands[0][2] == 'app.services.project_lifecycle'
    assert full_delete.commands[0][-1] == 'full_delete'
    assert full_delete.preview_paths == ['/srv/wsl-ops-panel']


def test_project_adapter_plans_systemd_runtime_autostart_and_notify() -> None:
    adapter = ProjectAdapter(
        project_dir='/srv/regmail-2api',
        current_version='main',
        service_unit='regmail-ui.service',
        config_root='/srv/panel/config',
        asset_snapshot_json='{"object_id":"project__regmail-2api","category":"project","name":"regmail-2api"}',
    )

    assert adapter.plan_action('start').commands == [['sudo', 'systemctl', 'start', 'regmail-ui.service']]
    assert adapter.plan_action('stop').commands == [['sudo', 'systemctl', 'stop', 'regmail-ui.service']]
    assert adapter.plan_action('restart').commands == [['sudo', 'systemctl', 'restart', 'regmail-ui.service']]
    assert adapter.plan_action('autostart_enable').commands == [['sudo', 'systemctl', 'enable', 'regmail-ui.service']]
    assert adapter.plan_action('autostart_disable').commands == [['sudo', 'systemctl', 'disable', 'regmail-ui.service']]

    notify = adapter.plan_action('notify_send')

    assert '-m' in notify.commands[0]
    assert 'app.services.notifications' in notify.commands[0]
    assert '--asset-id' in notify.commands[0]
    assert 'project__regmail-2api' in notify.commands[0]


def test_user_project_runtime_actions_control_all_related_units() -> None:
    adapter = ProjectAdapter(
        project_dir='/srv/oai-cpa-tools',
        service_unit='oai-cpa-tools.service',
        service_units=['oai-cpa-tools.service', 'freemail-pool.service'],
        service_scope='user',
    )

    assert adapter.plan_action('start').commands == [[
        'systemctl', '--user', 'start', 'oai-cpa-tools.service', 'freemail-pool.service'
    ]]
    assert adapter.plan_action('stop').commands == [[
        'systemctl', '--user', 'stop', 'oai-cpa-tools.service', 'freemail-pool.service'
    ]]
    assert adapter.plan_action('restart').requires_sudo is False
