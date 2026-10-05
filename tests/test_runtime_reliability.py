"""Regression cases from the October architecture review; no live resources."""
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.adapters.base import ActionPlan
from app.models.assets import DockerContainerSnapshot, RuntimeVersionInfo
from app.models.registry import ObjectDefinition, RegistrySnapshot
from app.services.assets import AssetService, build_docker_asset_snapshots
from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import InMemoryTaskStore
from app.tasks.worker import SerialTaskWorker
from app.terminals.system_terminal import SystemTerminalSink


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    config_at(tmp_path / 'config')


def config_at(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / 'categories').mkdir(exist_ok=True)
    (root / 'objects').mkdir(exist_ok=True)
    (root / 'panel.yaml').write_text(
        'title_cn: Test\ntitle_en: Test\nhost: 127.0.0.1\nport: 8328\n'
        'auth:\n  username: fixture\n  password: fixture\n'
    )
    return root


class VersionStub:
    def build_runtime_version_info(self, container):
        return RuntimeVersionInfo(image=container.image, image_tag=container.image_tag)


def container_at(folder, project):
    return DockerContainerSnapshot(
        id=project, name=project, image='fixture:v1', status='running', state='running',
        compose_project=project, compose_service='web', compose_working_dir=str(folder),
    )


def discovered(containers, deleted=()):
    return build_docker_asset_snapshots(
        RegistrySnapshot(categories=[], objects=[]), containers,
        version_service=VersionStub(), resolve_remote_versions=False,
        deleted_asset_ids=set(deleted),
    )


def test_discovered_project_id_survives_membership_changes(tmp_path):
    alpha, zeta = (container_at(tmp_path / 'shared', name) for name in ('alpha', 'zeta'))
    before = {a.primary_container_name: a.object_id for a in discovered([alpha, zeta])}
    after = discovered([zeta])
    assert after[0].object_id == before['zeta']
    assert after[0].object_id != before['alpha']


def test_delete_marker_only_hides_its_own_project(tmp_path):
    alpha, zeta = (container_at(tmp_path / 'shared', name) for name in ('alpha', 'zeta'))
    before = {a.primary_container_name: a.object_id for a in discovered([alpha, zeta])}
    after = discovered([alpha, zeta], [before['alpha']])
    assert [a.primary_container_name for a in after] == ['zeta']
    assert after[0].object_id == before['zeta']


def test_corrupt_plan_fails_one_task_and_worker_continues(tmp_path):
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store)
    bad = queue.enqueue('bad', 'start', plan=ActionPlan(commands=[]))
    Path(bad.plan_path).write_text('{corrupt')
    good = queue.enqueue('good', 'start', plan=ActionPlan(commands=[]))
    worker = SerialTaskWorker(queue=queue, sink=SystemTerminalSink(tmp_path / 'system.log'), poll_interval=.005)
    worker.start()
    try:
        deadline = monotonic() + 1
        while monotonic() < deadline and store.get(good.id).status in {'queued', 'running'} and worker._thread.is_alive():
            sleep(.005)
        assert store.get(bad.id).status == 'failed'
        assert store.get(bad.id).started_at is not None
        assert store.get(bad.id).finished_at >= store.get(bad.id).started_at
        assert store.get(good.id).status == 'succeeded'
        assert worker._thread.is_alive()
    finally:
        worker.stop()


def test_health_detects_stopped_worker(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(config_root=config_at(tmp_path / 'config'))
    response = TestClient(app).get('/healthz')
    assert response.status_code == 503
    assert response.json()['status'] != 'ok'


def test_scan_error_invalidates_observation_and_reconcile(tmp_path):
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    from app.services.agent_workbench import build_agent_workbench_context
    from app.services.agent_reconciler import AgentReconciler
    home = Path.home()
    (home / '.codex').mkdir()
    path = home / '.codex/config.toml'
    path.write_text('[mcp_servers.demo]\ncommand="node"\nargs=["demo.js"]\n')
    config = config_at(tmp_path / 'config')
    store = AgentMcpStore(agent_data_root(config))
    store.import_from_home(home, apps=['codex'])
    build_agent_workbench_context(config, home=home, which=lambda name: None)
    assert store.state.list_mcp_observations('__local__', 'codex')[0]['status'] == 'installed'
    path.write_text('[mcp_servers.demo\ninvalid TOML')
    context = build_agent_workbench_context(config, home=home, which=lambda name: None)
    assert 'codex' in context['agent_scan_errors']
    assert store.state.list_mcp_observations('__local__', 'codex')[0]['status'] == 'error'
    plan = AgentReconciler(agent_data_root(config)).plan_assignments(client_id='codex')
    assert 'codex:mcp:demo' not in plan.already_consistent
    assert plan.warnings
    assert not plan.operations, 'a failed read must not schedule a blind overwrite'


def test_importing_app_does_not_interrupt_existing_tasks(tmp_path):
    config_at(tmp_path / 'config')
    script = '''
from datetime import UTC, datetime
from app.tasks.store import SQLiteTaskStore
from app.models.tasks import TaskRecord
store = SQLiteTaskStore()
store.insert(TaskRecord(id='running', object_id='test', action='start', status='running', created_at=datetime.now(UTC), stdout_log_path='out', stderr_log_path='err'))
import app.main
assert store.get('running').status == 'running', store.get('running').status
'''
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_config_roots_isolate_tasks_plans_and_terminal_logs(tmp_path):
    from app.main import create_app
    first = create_app(config_root=config_at(tmp_path / 'first/config'))
    second = create_app(config_root=config_at(tmp_path / 'second/config'))
    task = first.state.task_queue.enqueue('fixture', 'start', plan=ActionPlan(commands=[]))
    assert not second.state.task_store.list_all()
    assert Path(task.plan_path).is_relative_to(tmp_path / 'first/data')
    assert first.state.system_terminal_sink.path.is_relative_to(tmp_path / 'first/data')
    assert second.state.system_terminal_sink.path.is_relative_to(tmp_path / 'second/data')


def test_bulk_action_scans_each_category_once(tmp_path, monkeypatch):
    from app.api.bulk_actions import BulkActionRequest, queue_bulk_action
    objects = [ObjectDefinition(id=f'item-{i}', category='docker', type='docker_compose', name=f'Item {i}',
                                config={'project_dir': str(tmp_path), 'compose_file': 'compose.yml', 'primary_container': f'web-{i}'}) for i in range(3)]
    registry = SimpleNamespace(snapshot=RegistrySnapshot(categories=[], objects=objects))
    scans = []
    def scan():
        scans.append(1)
        return []
    svc = AssetService(registry, docker_scanner=scan, config_root=tmp_path / 'config')
    queue = GlobalTaskQueue(InMemoryTaskStore())
    request = Request({'type': 'http', 'headers': [], 'app': SimpleNamespace(state=SimpleNamespace(asset_service=svc, task_queue=queue))})
    monkeypatch.setattr('app.api.bulk_actions.require_authenticated_request', lambda request: None)
    monkeypatch.setattr('app.api.assets.require_authenticated_request', lambda request: None)
    monkeypatch.setattr('app.api.assets._build_adapter', lambda *args: SimpleNamespace(plan_action=lambda *args, **kwargs: ActionPlan(commands=[])))
    response = queue_bulk_action('delete', BulkActionRequest(asset_ids=[o.id for o in objects]), request)
    assert response.queued_count == 3
    assert len(scans) == 1

@pytest.mark.parametrize('action', ['install', 'update', 'uninstall', 'sync'])
def test_mcp_scan_error_blocks_all_reconcile_writes(tmp_path, action):
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    from app.services.agent_reconciler import AgentReconciler
    from app.services.agent_workbench import build_agent_workbench_context
    home = Path.home()
    (home / '.codex').mkdir()
    path = home / '.codex/config.toml'
    path.write_text('[mcp_servers.demo]\ncommand="node"\n')
    config = tmp_path / 'config'
    store = AgentMcpStore(agent_data_root(config))
    store.import_from_home(home, apps=['codex'])
    path.write_text('[broken')
    build_agent_workbench_context(config, home=home, which=lambda _: None)
    reconciler = AgentReconciler(agent_data_root(config))
    plan = (reconciler.plan_assignments(client_id='codex') if action == 'sync' else
            reconciler.plan_resources('mcp', ['demo'], 'codex', action=action))
    assert not plan.operations
    assert not plan.already_consistent
    assert plan.warnings
    path.write_text('[mcp_servers.demo]\ncommand="node"\n')
    build_agent_workbench_context(config, home=home, which=lambda _: None)
    recovered = reconciler.plan_assignments(client_id='codex')
    assert recovered.already_consistent == ['codex:mcp:demo']
    assert not recovered.warnings


def test_invalid_json_import_preserves_assignments(tmp_path):
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    from app.services.agent_workbench import build_agent_workbench_context
    home = Path.home()
    config_file = home / '.claude.json'
    config_file.write_text('{"mcpServers":{"demo":{"command":"node"}}}')
    store = AgentMcpStore(agent_data_root(tmp_path / 'config'))
    store.import_from_home(home, apps=['claude'])
    before = store.state.list_mcp_assignments('__local__', 'claude')
    config_file.write_text('{broken')
    assert store.import_from_home(home, apps=['claude']) == 0
    assert store.state.list_mcp_assignments('__local__', 'claude') == before
    context = build_agent_workbench_context(tmp_path / 'config', home=home, which=lambda _: None)
    assert 'claude' in context['agent_scan_errors']
    assert store.state.list_mcp_observations('__local__', 'claude')[0]['status'] == 'error'


def test_login_uses_app_config_not_process_cwd(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    root = config_at(tmp_path / 'other/config')
    path = root / 'panel.yaml'
    path.write_text(path.read_text().replace('username: fixture', 'username: other-user').replace('password: fixture', 'password: other-password'))
    client = TestClient(create_app(config_root=root), follow_redirects=False)
    assert client.post('/auth/login', data={'username': 'fixture', 'password': 'fixture'}).status_code == 401
    assert client.post('/auth/login', data={'username': 'other-user', 'password': 'other-password'}).status_code == 302
    assert client.get('/api/terminals/debug').status_code == 200
    default = TestClient(create_app(config_root=tmp_path / 'config'), follow_redirects=False)
    default.cookies.update(client.cookies)
    assert default.get('/api/terminals/debug').status_code == 401


@pytest.mark.parametrize('damage', ['missing', 'schema', 'executor', 'sink'])
def test_worker_settles_other_failures_and_keeps_processing(tmp_path, monkeypatch, damage):
    import app.tasks.worker as worker_module
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store)
    bad = queue.enqueue('bad', 'start', plan=ActionPlan(commands=[]))
    good = queue.enqueue('good', 'start', plan=ActionPlan(commands=[]))
    if damage == 'missing':
        Path(bad.plan_path).unlink()
    elif damage == 'schema':
        Path(bad.plan_path).write_text('{"commands":123}')
    else:
        execute = worker_module.execute_action_plan
        def fail_first(task, plan, **kwargs):
            if task.id == bad.id:
                raise OSError('fixture failure')
            return execute(task, plan, **kwargs)
        monkeypatch.setattr(worker_module, 'execute_action_plan', fail_first)
    if damage == 'sink':
        monkeypatch.setattr(worker_module, 'append_task_chunk', lambda *a, **kw: (_ for _ in ()).throw(OSError('fixture log failure')))
    worker = SerialTaskWorker(queue=queue, sink=SystemTerminalSink(tmp_path / 'system.log'))
    worker._execute_task(bad)
    worker._execute_task(good)
    assert store.get(bad.id).status == 'failed'
    assert store.get(bad.id).finished_at
    assert store.get(good.id).status == 'succeeded'


def test_legacy_docker_id_never_resolves_from_stale_cache(tmp_path):
    from app.models.assets import AssetSnapshot
    registry = SimpleNamespace(snapshot=RegistrySnapshot(categories=[], objects=[]))
    service = AssetService(registry, docker_scanner=lambda: [], config_root=tmp_path / 'config')
    stale = AssetSnapshot(object_id='docker__old', category='docker', name='old', status='running')
    service._state_store.replace_asset_snapshots('docker', [stale])
    assert service.get_asset(stale.object_id) is None
    assert service.get_assets([stale.object_id]) == {}


def test_resource_plan_refreshes_mcp_without_opening_workbench(tmp_path, monkeypatch):
    from app.api.agent import AgentResourceRequest, _build_resource_plan
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    home = Path.home()
    (home / '.codex').mkdir()
    config_file = home / '.codex/config.toml'
    config_file.write_text('[mcp_servers.demo]\ncommand="node"\n')
    store = AgentMcpStore(agent_data_root(tmp_path / 'config'))
    store.import_from_home(home, apps=['codex'])
    config_file.write_text('[broken')
    request = Request({'type': 'http', 'headers': [], 'app': SimpleNamespace(state=SimpleNamespace(config_root=tmp_path / 'config'))})
    plan = _build_resource_plan(request, AgentResourceRequest(action='sync', resource_type='mcp', client_id='codex'))
    assert not plan.operations
    assert not plan.already_consistent
    assert plan.warnings


def test_profile_does_not_uninstall_after_failed_scan(tmp_path):
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    from app.services.agent_profiles import AgentProfileStore
    from app.services.agent_reconciler import AgentReconciler
    from app.services.agent_workbench import build_agent_workbench_context
    home = Path.home()
    (home / '.codex').mkdir()
    config_file = home / '.codex/config.toml'
    config_file.write_text('[mcp_servers.demo]\ncommand="node"\n')
    root = agent_data_root(tmp_path / 'config')
    store = AgentMcpStore(root)
    store.import_from_home(home, apps=['codex'])
    profiles = AgentProfileStore(root)
    profiles.upsert('empty', name='empty', items=[])
    config_file.write_text('[broken')
    build_agent_workbench_context(tmp_path / 'config', home=home, which=lambda _: None)
    plan = AgentReconciler(root).plan_profile('empty', client_id='codex')
    assert not plan.operations
    assert not plan.already_consistent
    assert plan.warnings


def test_stopped_app_does_not_close_another_apps_terminals(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    first = create_app(config_root=config_at(tmp_path / 'first/config'))
    second = create_app(config_root=config_at(tmp_path / 'second/config'))
    closed = []
    monkeypatch.setattr(first.state.debug_terminal_manager, 'close_all', lambda: closed.append('first'))
    monkeypatch.setattr(second.state.debug_terminal_manager, 'close_all', lambda: closed.append('second'))
    with TestClient(first) as client:
        assert client.get('/healthz').status_code == 200
    assert closed == ['first']
    assert TestClient(first).get('/healthz').status_code == 503


def test_recovery_only_runs_when_lifespan_starts(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    store = InMemoryTaskStore(seed_running=True)
    app = create_app(config_root=tmp_path / 'config', task_store=store)
    assert store.get('seed-running-task').status == 'running'
    with TestClient(app):
        assert store.get('seed-running-task').status == 'interrupted'


@pytest.mark.parametrize('client,path,text', [
    ('claude', '.claude.json', '{"mcpServers":[]}'),
    ('claude', '.claude.json', '[]'),
    ('codex', '.codex/config.toml', 'mcp_servers="bad"'),
    ('claude', '.claude.json', '{"mcpServers":{"demo":"bad"}}'),
])
def test_malformed_mcp_structure_is_error_not_empty_installation(tmp_path, client, path, text):
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    home = Path.home()
    native = home / path
    native.parent.mkdir(parents=True, exist_ok=True)
    native.write_text(text)
    store = AgentMcpStore(agent_data_root(tmp_path / 'config'))
    with pytest.raises(ValueError):
        store.refresh_observations(home, client)
    assert store.state.get_mcp_scan_state('__local__', client)['status'] == 'error'


def test_legacy_tombstone_does_not_hide_new_stable_project(tmp_path):
    rows = discovered([container_at(tmp_path / 'shared', 'alpha')], ['docker__shared'])
    assert len(rows) == 1
    assert rows[0].object_id != 'docker__shared'


def test_bulk_deduplicates_and_keeps_json_for_htmx(tmp_path, monkeypatch):
    from app.api.bulk_actions import BulkActionRequest, queue_bulk_action
    registry = SimpleNamespace(snapshot=RegistrySnapshot(categories=[], objects=[
        ObjectDefinition(id='demo', category='docker', type='docker_compose', name='demo',
                         config={'project_dir': str(tmp_path), 'compose_file': 'compose.yml', 'primary_container': 'web'})
    ]))
    service = AssetService(registry, docker_scanner=lambda: [], config_root=tmp_path / 'config')
    queue = GlobalTaskQueue(InMemoryTaskStore())
    request = Request({'type': 'http', 'headers': [(b'hx-request', b'true')],
                       'app': SimpleNamespace(state=SimpleNamespace(asset_service=service, task_queue=queue))})
    monkeypatch.setattr('app.api.assets.require_authenticated_request', lambda _: None)
    monkeypatch.setattr('app.api.bulk_actions.require_authenticated_request', lambda _: None)
    monkeypatch.setattr('app.api.assets._build_adapter', lambda *a: SimpleNamespace(plan_action=lambda *a, **kw: ActionPlan(commands=[])))
    result = queue_bulk_action('delete', BulkActionRequest(asset_ids=['demo', 'missing', 'demo']), request)
    assert result.queued_count == 1
    assert len(queue.store.list_all()) == 1
    assert [item.asset_id for item in result.skipped] == ['missing']


def test_direct_mcp_install_scan_error_is_persisted(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.core.security import COOKIE_NAME, issue_session_token
    from app.services.agent_mcp import AgentMcpStore, agent_data_root
    home = Path.home()
    (home / '.codex').mkdir()
    native = home / '.codex/config.toml'
    native.write_text('[mcp_servers.demo]\ncommand="node"\n')
    config = tmp_path / 'config'
    store = AgentMcpStore(agent_data_root(config))
    store.import_from_home(home, apps=['codex'])
    native.write_text('[broken')
    client = TestClient(create_app(config_root=config))
    client.cookies.set(COOKIE_NAME, issue_session_token(config_root=config))
    response = client.post('/api/agent/mcp/local/install', json={'client_id': 'codex', 'mcp_ids': ['demo']})
    assert response.status_code == 400
    assert store.state.list_mcp_observations('__local__', 'codex')[0]['status'] == 'error'
    assert native.read_text() == '[broken'
    sync = client.post('/api/agent/resources/reconcile', json={'action': 'sync', 'resource_type': 'mcp', 'client_id': 'codex'})
    assert sync.status_code == 200
    assert sync.json()['verified'] is False
    assert sync.json()['warnings']
    assert not sync.json()['operations']
    assert not sync.json()['already_consistent']
    assert native.read_text() == '[broken'
