from app.adapters.systemd_adapter import SystemdUnitAdapter


def test_delete_maps_to_disable_now() -> None:
    adapter = SystemdUnitAdapter(unit_name='cftunnel.service', working_dir='/srv/cftunnel')

    plan = adapter.plan_action('delete')

    assert plan.commands == [['sudo', 'systemctl', 'disable', '--now', 'cftunnel.service']]
    assert plan.requires_sudo is True
    assert plan.preview_objects == ['cftunnel.service']
    assert plan.preview_paths == ['/srv/cftunnel']


def test_full_delete_is_disabled_for_systemd() -> None:
    adapter = SystemdUnitAdapter(unit_name='cftunnel.service', working_dir='/srv/cftunnel')

    try:
        adapter.plan_action('full_delete')
    except ValueError as exc:
        assert 'phase 1' in str(exc)
    else:  # pragma: no cover
        raise AssertionError('expected full_delete to be rejected')


def test_systemd_adapter_plans_runtime_and_autostart_actions() -> None:
    adapter = SystemdUnitAdapter(unit_name='wsl-ops-panel.service', working_dir='/srv/panel')

    assert adapter.plan_action('start').commands == [['sudo', 'systemctl', 'start', 'wsl-ops-panel.service']]
    assert adapter.plan_action('stop').commands == [['sudo', 'systemctl', 'stop', 'wsl-ops-panel.service']]
    assert adapter.plan_action('restart').commands == [['sudo', 'systemctl', 'restart', 'wsl-ops-panel.service']]
    assert adapter.plan_action('autostart_enable').commands == [['sudo', 'systemctl', 'enable', 'wsl-ops-panel.service']]
    assert adapter.plan_action('autostart_disable').commands == [['sudo', 'systemctl', 'disable', 'wsl-ops-panel.service']]
