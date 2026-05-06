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
