from app.adapters.python_adapter import PythonPackageAdapter


def test_python_adapter_builds_deploy_and_full_delete_plans() -> None:
    adapter = PythonPackageAdapter(package_name='fastapi', current_version='0.115.0', full_delete_paths=['/tmp/fastapi-cache'])

    deploy_plan = adapter.plan_action('deploy_version', version='0.116.0')
    delete_plan = adapter.plan_action('delete')
    full_delete_plan = adapter.plan_action('full_delete')

    assert deploy_plan.commands == [['python3', '-m', 'pip', 'install', 'fastapi==0.116.0']]
    assert delete_plan.commands == [['python3', '-m', 'pip', 'uninstall', '-y', 'fastapi']]
    assert full_delete_plan.preview_paths == ['/tmp/fastapi-cache']
