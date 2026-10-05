from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.adapters.base import ActionPlan
from app.models.registry import ObjectDefinition, RegistrySnapshot
from app.services.assets import AssetService
from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import InMemoryTaskStore
from tests.test_docker_lifecycle_repair import FakeDocker, container, context
from tests.test_runtime_reliability import config_at


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    config_at(tmp_path / 'config')


def test_mcp_named_scan_is_a_resource_not_a_scan_error(tmp_path):
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    from app.services.agent_workbench import build_agent_workbench_context
    home = Path.home()
    (home / '.codex').mkdir()
    (home / '.codex/config.toml').write_text('[mcp_servers.__scan__]\ncommand="node"\n')
    config = tmp_path / 'config'
    store = AgentMcpStore(agent_data_root(config))
    store.import_from_home(home, apps=['codex'])
    page = build_agent_workbench_context(config, home=home, which=lambda _: None)
    assert page['agent_scan_errors'] == {}
    assert store.state.list_mcp_observations('__local__', 'codex')[0]['mcp_id'] == '__scan__'


def test_bulk_scan_failure_does_not_block_healthy_category(tmp_path, monkeypatch):
    from app.api.bulk_actions import queue_bulk_action, BulkActionRequest
    objects = [
        ObjectDefinition(id='good', category='docker', type='docker_compose', name='good',
                         config={'project_dir':str(tmp_path),'compose_file':'compose.yml','primary_container':'web'}),
        ObjectDefinition(id='bad', category='systemd', type='systemd_unit', name='bad',
                         config={'unit_name':'bad.service','working_dir':str(tmp_path)}),
    ]
    registry = SimpleNamespace(snapshot=RegistrySnapshot(categories=[], objects=objects))
    def broken_scan():
        raise PermissionError('fixture scan failure')
    service = AssetService(registry, docker_scanner=lambda: [], systemd_scanner=broken_scan, config_root=tmp_path/'config')
    request = Request({'type':'http','headers':[], 'app':SimpleNamespace(state=SimpleNamespace(asset_service=service,task_queue=GlobalTaskQueue(InMemoryTaskStore())))})
    monkeypatch.setattr('app.api.bulk_actions.require_authenticated_request',lambda _:None)
    monkeypatch.setattr('app.api.assets.require_authenticated_request',lambda _:None)
    monkeypatch.setattr('app.api.assets._build_adapter',lambda *a:SimpleNamespace(plan_action=lambda *a,**k:ActionPlan(commands=[])))
    result = queue_bulk_action('delete',BulkActionRequest(asset_ids=['bad','good']),request)
    assert result.queued_count == 1
    assert result.tasks[0].object_id == 'good'
    assert [item.asset_id for item in result.skipped] == ['bad']
    assert 'PermissionError' in result.skipped[0].reason


@pytest.mark.parametrize('action',['check','start','delete','full_delete'])
def test_captured_container_identity_rejects_new_unseen_names(tmp_path, action):
    from app.services.docker_lifecycle import execute
    project = tmp_path / 'project'
    project.mkdir()
    captured = context(project)
    captured['containers'] = [{'name':'demo','id':'a'*64}]
    docker = FakeDocker([container(name='renamed',id='b'*64,cwd=str(project))])
    with pytest.raises(ValueError,match='身份|变化'):
        execute(captured,action,runner=docker,verify_timeout=0)
    assert all(cmd[:3] in (['docker','ps','-aq'],['docker','container','inspect']) for cmd in docker.commands)


def test_worker_recovers_after_transient_store_read_error(tmp_path, monkeypatch):
    import sqlite3
    from time import monotonic, sleep
    from app.tasks.worker import SerialTaskWorker
    from app.terminals.system_terminal import SystemTerminalSink
    queue = GlobalTaskQueue(InMemoryTaskStore())
    task = queue.enqueue('fixture', 'start', plan=ActionPlan(commands=[]))
    peek = queue.peek_next_queued_task
    calls = 0

    def flaky_peek():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError('database is locked')
        return peek()

    monkeypatch.setattr(queue, 'peek_next_queued_task', flaky_peek)
    worker = SerialTaskWorker(queue=queue, sink=SystemTerminalSink(tmp_path / 'system.log'), poll_interval=.005)
    worker.start()
    try:
        deadline = monotonic() + 2
        while monotonic() < deadline and queue.store.get(task.id).status in {'queued', 'running'}:
            sleep(.005)
        assert worker.is_alive
        assert queue.store.get(task.id).status == 'succeeded'
    finally:
        worker.stop()


def test_bulk_detail_failure_is_per_asset_and_does_not_expose_raw_error(tmp_path, monkeypatch):
    objects = [ObjectDefinition(id=name, category='systemd', type='systemd_unit', name=name,
               config={'unit_name': f'{name}.service', 'working_dir': str(tmp_path)})
               for name in ('bad', 'good')]
    calls = []
    service = AssetService(SimpleNamespace(snapshot=RegistrySnapshot(categories=[], objects=objects)),
                           systemd_scanner=lambda: calls.append(1) or [], config_root=tmp_path / 'config')

    def enrich(asset):
        if asset.object_id == 'bad':
            raise ValueError('fixture-private-value')
        return asset

    monkeypatch.setattr(service, '_enrich_detail_asset', enrich)
    errors = {}
    assets = service.get_assets(['bad', 'good', 'good'], errors=errors)
    assert set(assets) == {'good'}
    assert calls == [1]
    assert errors == {'bad': '资产详情读取失败 (ValueError)'}
