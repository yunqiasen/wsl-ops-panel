from app.adapters.system_adapter import SystemInfrastructureAdapter


def test_apt_update_latest_only_refreshes_index_and_lists_upgradable_packages() -> None:
    adapter = SystemInfrastructureAdapter(name='APT / 系统软件包', current_version='12 upgradable')

    plan = adapter.plan_action('update_latest')

    assert plan.commands == [['sudo', 'apt', 'update'], ['apt', 'list', '--upgradable']]
    assert plan.requires_sudo is True
    assert plan.preview_objects == ['APT / 系统软件包', 'check-upgradable']


def test_apt_deploy_version_is_not_used_for_system_upgrade() -> None:
    adapter = SystemInfrastructureAdapter(name='APT / 系统软件包', current_version='12 upgradable')

    try:
        adapter.plan_action('deploy_version', version='upgrade')
    except ValueError as exc:
        assert 'apt upgrade is intentionally not exposed' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('expected apt deploy_version to be rejected')
