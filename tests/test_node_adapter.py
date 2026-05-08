from app.adapters.node_adapter import NodePackageAdapter


def test_node_adapter_builds_update_and_delete_plans() -> None:
    adapter = NodePackageAdapter(package_name='update', current_version='0.7.4', full_delete_paths=['/tmp/update-cache'])

    update_plan = adapter.plan_action('update_latest')
    delete_plan = adapter.plan_action('delete')
    full_delete_plan = adapter.plan_action('full_delete')

    assert update_plan.commands == [['npm', 'install', '-g', 'update@latest']]
    assert delete_plan.commands == [['npm', 'uninstall', '-g', 'update']]
    assert full_delete_plan.preview_paths == ['/tmp/update-cache']
    assert full_delete_plan.commands[-1] == ['rm', '-rf', '/tmp/update-cache']
