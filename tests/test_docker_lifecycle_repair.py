import json
from pathlib import Path

import pytest

from app.adapters.docker_adapter import DockerComposeAdapter, _OVERRIDE_DEPLOY_SCRIPT
from app.scanners.docker_scanner import _parse_labels
from app.terminals.system_terminal import iter_sse_events


def test_labels_preserve_multiple_compose_files():
    labels = _parse_labels('com.docker.compose.project.config_files=/资源/a.yml,/资源/b.yml,com.docker.compose.project=app')
    assert labels['com.docker.compose.project.config_files'] == '/资源/a.yml,/资源/b.yml'


def test_labels_accept_structured_mapping():
    assert _parse_labels({'project': 'a,b'}) == {'project': 'a,b'}


def test_version_script_is_executable_python():
    compile(_OVERRIDE_DEPLOY_SCRIPT, '<deploy-version>', 'exec')


def test_tail_inside_chinese_character_keeps_stream_alive(tmp_path):
    path = tmp_path / 'log'
    path.write_text('中文日志', encoding='utf-8')
    events = iter_sse_events(path, initial_tail_bytes=5)
    event = next(events)
    assert '志' in event
    events.close()


def test_log_rotation_to_larger_file_resets_offset(tmp_path):
    path = tmp_path / 'log'
    path.write_text('old')
    events = iter_sse_events(path, poll_interval=0, idle_heartbeat=1)
    next(events)
    path.rename(tmp_path / 'old')
    path.write_text('new log content')
    assert 'new log content' in next(events)
    events.close()


def test_runtime_start_does_not_depend_on_project_cwd():
    adapter = DockerComposeAdapter(project_dir='/gone', compose_file='compose.yml',
        primary_container='demo', compose_service='web',
        runtime_context={'project': 'original', 'containers': []})
    plan = adapter.plan_action('start')
    assert plan.working_dir != '/gone'
    assert 'app.services.docker_lifecycle' in plan.commands[0]

class FakeDocker:
    def __init__(self, containers):
        self.containers = containers
        self.commands = []

    def __call__(self, command, **kwargs):
        import subprocess
        self.commands.append(command)
        out = ''
        code = 0
        if command[:3] == ['docker', 'ps', '-aq']:
            out = '\n'.join(c['Id'] for c in self.containers)
        elif command[:3] == ['docker', 'container', 'inspect']:
            out = json.dumps([c for c in self.containers if c['Id'] in command[3:]])
        elif command[:2] in (['docker', 'start'], ['docker', 'restart'], ['docker', 'stop']):
            for c in self.containers:
                if c['Id'] in command[2:]:
                    c['State']['Running'] = command[1] != 'stop'
        elif command[:2] == ['docker', 'compose'] and 'up' in command:
            for c in self.containers:
                c['State']['Running'] = True
        elif command[:3] == ['docker', 'rm', '-f']:
            self.containers[:] = [c for c in self.containers if c['Id'] not in command[3:]]
        elif command[:3] in (['docker', 'network', 'ls'], ['docker', 'volume', 'ls']):
            out = ''
        return subprocess.CompletedProcess(command, code, out, '')


def container(project='original', name='demo', id='a' * 64, cwd='/tmp/panel-missing/project'):
    return {'Id': id, 'Name': '/' + name, 'Image': 'sha256:image',
            'Config': {'Labels': {'com.docker.compose.project': project,
                'com.docker.compose.service': 'web',
                'com.docker.compose.project.working_dir': cwd}},
            'State': {'Running': False}, 'Mounts': []}


def context(tmp_path=None):
    return {'project': 'original', 'project_dir': str(tmp_path or '/tmp/panel-missing/project'),
            'compose_files': ['compose.yml'], 'primary_container': 'demo',
            'compose_service': 'web', 'containers': []}


def test_existing_start_ignores_missing_compose_and_other_project():
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container(), container(project='other', name='other', id='b' * 64)])
    execute(context(), 'start', runner=docker, verify_timeout=0)
    assert docker.containers[0]['State']['Running']
    assert not docker.containers[1]['State']['Running']
    assert not any(c[:2] == ['docker', 'compose'] for c in docker.commands)


def test_delete_works_after_directory_disappeared():
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container()])
    execute(context(), 'full_delete', runner=docker, verify_timeout=0)
    assert not docker.containers


def test_update_uses_project_multifile_and_prechecks_before_pull(tmp_path):
    from app.services.docker_lifecycle import execute
    for name in ['base.yml', 'override.yml']:
        (tmp_path / name).write_text('services: {}')
    ctx = context(tmp_path)
    ctx['compose_files'] = ['base.yml', 'override.yml']
    docker = FakeDocker([container(cwd=str(tmp_path))])
    execute(ctx, 'update_latest', runner=docker, verify_timeout=0)
    commands = [c for c in docker.commands if c[:2] == ['docker', 'compose']]
    assert commands[0][-2:] == ['config', '--quiet']
    assert commands[0][2:4] == ['-p', 'original']
    assert commands[0].count('-f') == 2
    assert any('pull' in c for c in commands)


def test_missing_compose_update_fails_before_mutation(tmp_path):
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container(cwd=str(tmp_path))])
    with pytest.raises(ValueError, match='Compose'):
        execute(context(tmp_path), 'update_latest', runner=docker)
    assert not any('pull' in c or 'up' in c for c in docker.commands)


def test_no_success_when_docker_start_returns_zero_but_container_exits():
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container()])
    def runner(command, **kwargs):
        result = docker(command, **kwargs)
        docker.containers[0]['State']['Running'] = False
        return result
    with pytest.raises(RuntimeError, match='状态'):
        execute(context(), 'start', runner=runner, verify_timeout=0)


def test_full_delete_preserves_shared_working_directory(tmp_path):
    from app.services.docker_lifecycle import execute
    (tmp_path / 'keep').write_text('other project data')
    docker = FakeDocker([container(cwd=str(tmp_path)), container(project='other', name='other', id='b'*64, cwd=str(tmp_path))])
    execute(context(tmp_path), 'full_delete', runner=docker, verify_timeout=0)
    assert (tmp_path / 'keep').exists()
    assert len(docker.containers) == 1


def test_full_delete_rejects_symlink_before_removing_containers(tmp_path):
    from app.services.docker_lifecycle import execute
    (tmp_path / 'actual').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'actual', target_is_directory=True)
    docker = FakeDocker([container(cwd=str(tmp_path / 'link'))])
    with pytest.raises(ValueError, match='路径'):
        execute(context(tmp_path / 'link'), 'full_delete', runner=docker)
    assert docker.containers


def test_discovery_separates_projects_sharing_one_directory(tmp_path):
    from app.models.assets import DockerContainerSnapshot
    from app.models.registry import RegistrySnapshot
    from app.services.assets import build_docker_asset_snapshots
    snapshots = [DockerContainerSnapshot(id=str(i), name=f'web-{i}', image='demo:v1', status='Up',
        compose_project=f'project-{i}', compose_service='web', compose_working_dir=str(tmp_path)) for i in (1, 2)]
    assets = build_docker_asset_snapshots(RegistrySnapshot(categories=[], objects=[]), snapshots, resolve_remote_versions=False, deleted_asset_ids=set())
    assert len(assets) == 2
    assert all(len(a.containers) == 1 for a in assets)


def test_permission_failure_happens_before_container_deletion(tmp_path, monkeypatch):
    from app.services.docker_lifecycle import execute
    import subprocess
    docker = FakeDocker([container(cwd=str(tmp_path))])
    monkeypatch.setattr('app.services.docker_lifecycle.os.access', lambda *a: False)
    def runner(cmd, **kw):
        if cmd[0] == 'sudo':
            return subprocess.CompletedProcess(cmd, 1, '', 'password required')
        return docker(cmd, **kw)
    with pytest.raises(ValueError, match='权限预检'):
        execute(context(tmp_path), 'full_delete', runner=runner)
    assert docker.containers


def test_sse_does_not_acknowledge_partial_character(tmp_path):
    path = tmp_path / 'log'
    path.write_bytes(b'abc' + '中'.encode()[:1])
    events = iter_sse_events(path, poll_interval=0, idle_heartbeat=1)
    assert 'id: 3\n' in next(events)
    with path.open('ab') as f:
        f.write('中'.encode()[1:])
    assert '中' in next(events)
    events.close()


def test_bulk_feedback_includes_backend_skipped_reasons():
    js = Path('app/static/app.js').read_text()
    assert 'payload.skipped' in js
    assert 'item.reason' in js


def test_successful_stderr_is_not_labeled_error(tmp_path):
    from app.tasks.executor import _append_output_summary
    from app.terminals.system_terminal import SystemTerminalSink
    from app.models.tasks import TaskRecord
    from datetime import datetime, UTC
    sink = SystemTerminalSink(tmp_path / 'system.log')
    task = TaskRecord(id='sample', object_id='demo', action='start', status='running',
        created_at=datetime.now(UTC), stdout_log_path=str(tmp_path / 'out'), stderr_log_path=str(tmp_path / 'err'))
    _append_output_summary(task, 'Container demo Started\n', sink=sink, stream='stderr', failed=False)
    assert '报错' not in sink.path.read_text()


def test_shared_volume_is_retained(tmp_path):
    from app.services.docker_lifecycle import execute
    import subprocess
    own = container(cwd=str(tmp_path))
    other = container(project='other', name='other', id='b'*64, cwd='/tmp/other')
    other['Mounts'] = [{'Type': 'volume', 'Name': 'shared-data'}]
    docker = FakeDocker([own, other])
    def runner(cmd, **kw):
        if cmd[:3] == ['docker', 'volume', 'ls']:
            return subprocess.CompletedProcess(cmd, 0, 'shared-data\n', '')
        if cmd[:3] == ['docker', 'volume', 'inspect']:
            return subprocess.CompletedProcess(cmd, 0, '[{"Name":"shared-data"}]', '')
        return docker(cmd, **kw)
    execute(context(tmp_path), 'full_delete', runner=runner, verify_timeout=0)
    assert ['docker', 'volume', 'rm', 'shared-data'] not in docker.commands
    assert other in docker.containers


def test_container_replacement_is_detected_before_mutation():
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container()])
    ctx = context()
    ctx['containers'] = [{'name': 'demo', 'id': 'b'*64}]
    with pytest.raises(ValueError, match='身份'):
        execute(ctx, 'start', runner=docker)
    assert not docker.containers[0]['State']['Running']


def test_first_deployment_discovers_generated_container_name(tmp_path):
    from app.services.docker_lifecycle import execute
    import subprocess
    (tmp_path / 'compose.yml').write_text('services: {}')
    ctx = context(tmp_path)
    ctx['project'] = None
    ctx['primary_container'] = 'web'
    docker = FakeDocker([])
    def runner(cmd, **kw):
        if cmd[:2] == ['docker', 'compose'] and 'up' in cmd:
            c = container(project='generated-project', name='generated-project-web-1', cwd=str(tmp_path))
            c['State']['Running'] = True
            docker.containers.append(c)
        if cmd[:2] == ['docker', 'compose'] and cmd[-2:] == ['ps', '-aq']:
            return subprocess.CompletedProcess(cmd, 0, 'a'*64, '')
        return docker(cmd, **kw)
    execute(ctx, 'start', runner=runner, verify_timeout=0)


def test_structured_scanner_keeps_unambiguous_labels(monkeypatch):
    from app.scanners.docker_scanner import scan_docker_containers
    import subprocess
    def runner(cmd, **kwargs):
        if cmd[:2] == ['docker', 'ps']:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'ID': 'a'*12, 'Names': 'demo', 'Image': 'demo:v1', 'Labels': 'key=wrong'}), '')
        return subprocess.CompletedProcess(cmd, 0, json.dumps('a'*64) + ' ' + json.dumps({'key': 'a,b=c'}), '')
    monkeypatch.setattr('app.scanners.docker_scanner.subprocess.run', runner)
    assert scan_docker_containers()[0].labels['key'] == 'a,b=c'


def test_registered_and_discovered_api_adapters_both_use_runtime_context(tmp_path):
    from types import SimpleNamespace
    from app.api.assets import _build_docker_adapter, _build_discovered_docker_adapter
    from app.models.assets import AssetSnapshot, DockerContainerSnapshot
    from app.models.registry import ObjectDefinition
    obj = ObjectDefinition(id='demo', category='docker', type='docker_compose', name='Demo',
        config={'project_dir': str(tmp_path), 'compose_file': 'base.yml', 'primary_container': 'demo', 'compose_service': 'web'})
    c = DockerContainerSnapshot(id='a'*64, name='demo', image='repo/image:v1', status='Up', compose_service='web',
        compose_project='original', labels={'com.docker.compose.project.config_files': '/base.yml,/override.yml'})
    asset = AssetSnapshot(object_id='demo', category='docker', name='Demo', status='Up', containers=[c],
        primary_container_name='demo', metadata=obj.config)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(config_root=tmp_path)))
    for adapter in (_build_docker_adapter(request, obj, asset), _build_discovered_docker_adapter(request, asset)):
        plan = adapter.plan_action('start')
        assert 'app.services.docker_lifecycle' in plan.commands[0]
        assert adapter.runtime_context['project'] == 'original'
        assert adapter.runtime_context['compose_files'] == ['/base.yml', '/override.yml']


def test_full_delete_preserves_nested_project_directory(tmp_path):
    from app.services.docker_lifecycle import execute
    child = tmp_path / 'other'
    child.mkdir()
    (child / 'keep').write_text('data')
    docker = FakeDocker([container(cwd=str(tmp_path)), container(project='other', name='other', id='b'*64, cwd=str(child))])
    execute(context(tmp_path), 'full_delete', runner=docker, verify_timeout=0)
    assert (child / 'keep').exists()


def test_full_delete_without_identity_preserves_directory(tmp_path):
    from app.services.docker_lifecycle import execute
    (tmp_path / 'keep').write_text('data')
    execute(context(tmp_path), 'full_delete', runner=FakeDocker([]), verify_timeout=0)
    assert (tmp_path / 'keep').exists()


def test_unreadable_walk_preflights_sudo_before_delete(tmp_path, monkeypatch):
    from app.services.docker_lifecycle import execute
    import subprocess
    docker = FakeDocker([container(cwd=str(tmp_path))])
    def walk(path, *, followlinks=False, onerror=None):
        if onerror:
            onerror(PermissionError('unreadable'))
        return iter([])
    monkeypatch.setattr('app.services.docker_lifecycle.os.walk', walk)
    def runner(cmd, **kw):
        if cmd[0] == 'sudo':
            return subprocess.CompletedProcess(cmd, 1, '', '')
        return docker(cmd, **kw)
    with pytest.raises(ValueError, match='权限预检'):
        execute(context(tmp_path), 'full_delete', runner=runner)
    assert docker.containers


def test_restarting_container_does_not_pass_start_verification():
    from app.services.docker_lifecycle import execute
    c = container()
    c['State'].update(Running=True, Restarting=True)
    with pytest.raises(RuntimeError, match='状态'):
        execute(context(), 'start', runner=FakeDocker([c]), verify_timeout=0)


def test_deploy_version_keeps_reusable_override(tmp_path):
    from app.services.docker_lifecycle import execute
    (tmp_path / 'compose.yml').write_text('services: {}')
    ctx = context(tmp_path)
    ctx['image_repository'] = 'repo/image'
    docker = FakeDocker([container(cwd=str(tmp_path))])
    execute(ctx, 'deploy_version', version='v2', runner=docker, verify_timeout=0)
    deploy = next(c for c in docker.commands if 'up' in c)
    files = [deploy[i+1] for i, v in enumerate(deploy) if v == '-f']
    assert all(Path(file).exists() for file in files)
    ctx['compose_files'] = files
    execute(ctx, 'update_latest', runner=docker, verify_timeout=0)


def test_deployment_failure_restores_override_configuration(tmp_path):
    from app.services.docker_lifecycle import execute
    import subprocess
    (tmp_path / 'compose.yml').write_text('services: {}')
    override = tmp_path / '.wsl-ops-panel.version.json'
    previous = b'{"services":{"web":{"image":"repo/image:v1"}}}'
    override.write_bytes(previous)
    ctx = context(tmp_path)
    ctx['image_repository'] = 'repo/image'
    docker = FakeDocker([container(cwd=str(tmp_path))])
    def runner(cmd, **kw):
        if 'up' in cmd:
            return subprocess.CompletedProcess(cmd, 1, '', 'failed deploy')
        return docker(cmd, **kw)
    with pytest.raises(RuntimeError):
        execute(ctx, 'deploy_version', version='v2', runner=runner, verify_timeout=0)
    assert override.read_bytes() == previous


def test_completed_dependency_is_not_restarted_and_not_required_to_run():
    from app.services.docker_lifecycle import execute
    init = container(name='init', id='b'*64)
    init['Config']['Labels']['com.docker.compose.service'] = 'init'
    init['State'].update(Status='exited', ExitCode=0)
    web = container()
    web['Config']['Labels']['com.docker.compose.depends_on'] = 'init:service_completed_successfully:true'
    docker = FakeDocker([web, init])
    execute(context(), 'start', runner=docker, verify_timeout=0)
    assert ['docker', 'start', 'b'*64] not in docker.commands


def test_network_pull_retries_without_repeating_up(tmp_path, monkeypatch):
    from app.services.docker_lifecycle import execute
    import subprocess
    monkeypatch.setattr('app.services.docker_lifecycle.time.sleep', lambda n: None)
    (tmp_path / 'compose.yml').write_text('services: {}')
    docker = FakeDocker([container(cwd=str(tmp_path))])
    pulls = []
    def runner(cmd, **kw):
        if 'pull' in cmd:
            pulls.append(cmd)
            if len(pulls) == 1:
                return subprocess.CompletedProcess(cmd, 1, '', 'TLS handshake timeout')
        return docker(cmd, **kw)
    execute(context(tmp_path), 'update_latest', runner=runner, verify_timeout=0)
    assert len(pulls) == 2
    assert len([cmd for cmd in docker.commands if 'up' in cmd]) == 1


def test_registered_primary_does_not_select_other_project_same_service(tmp_path):
    from app.models.assets import DockerContainerSnapshot
    from app.models.registry import RegistrySnapshot, ObjectDefinition
    from app.services.assets import build_docker_asset_snapshots
    snapshots = [DockerContainerSnapshot(id=str(i), name=f'web-{i}', image='demo:v1', status='Up',
        compose_project=f'project-{i}', compose_service='web', compose_working_dir=str(tmp_path)) for i in (1, 2)]
    obj = ObjectDefinition(id='registered', category='docker', type='docker_compose', name='Demo',
        config={'project_dir':str(tmp_path),'compose_file':'compose.yml','primary_container':'web-2','compose_service':'web'})
    assets = build_docker_asset_snapshots(RegistrySnapshot(categories=[], objects=[obj]), snapshots,
        resolve_remote_versions=False, deleted_asset_ids=set())
    registered = next(a for a in assets if a.object_id=='registered')
    assert registered.primary_container_name == 'web-2'
    assert [c.name for c in registered.containers] == ['web-2']
    assert any(a.primary_container_name == 'web-1' for a in assets if a.object_id!='registered')


def test_missing_registered_primary_does_not_adopt_other_project(tmp_path):
    from app.models.assets import DockerContainerSnapshot
    from app.models.registry import RegistrySnapshot, ObjectDefinition
    from app.services.assets import build_docker_asset_snapshots
    snapshot = DockerContainerSnapshot(id='1', name='other', image='demo:v1', status='Up',
        compose_project='other', compose_service='web', compose_working_dir=str(tmp_path))
    obj = ObjectDefinition(id='registered', category='docker', type='docker_compose', name='Demo',
        config={'project_dir':str(tmp_path),'compose_file':'compose.yml','primary_container':'gone','compose_service':'web'})
    assets = build_docker_asset_snapshots(RegistrySnapshot(categories=[], objects=[obj]), [snapshot],
        resolve_remote_versions=False, deleted_asset_ids=set())
    registered = next(a for a in assets if a.object_id=='registered')
    assert registered.containers == []
    assert registered.primary_container_name is None
    assert any(a.primary_container_name == 'other' for a in assets if a.object_id!='registered')


def test_start_deploys_missing_application_even_if_dependency_exists(tmp_path):
    from app.services.docker_lifecycle import execute
    (tmp_path / 'compose.yml').write_text('services: {}')
    dependency = container(name='database', id='b'*64, cwd=str(tmp_path))
    dependency['Config']['Labels']['com.docker.compose.service'] = 'database'
    docker = FakeDocker([dependency])
    def runner(cmd, **kwargs):
        if cmd[:2] == ['docker', 'compose'] and 'up' in cmd:
            docker.containers.append(container(cwd=str(tmp_path)))
        return docker(cmd, **kwargs)
    execute(context(tmp_path), 'start', runner=runner, verify_timeout=0)
    assert any('up' in cmd for cmd in docker.commands)


def test_explicit_base_files_keep_persisted_version_override(tmp_path):
    from app.services.docker_lifecycle import execute
    (tmp_path / 'compose.yml').write_text('services: {}')
    override = tmp_path / '.wsl-ops-panel.version.json'
    override.write_text('{"services":{"web":{"image":"repo/image:v2"}}}')
    docker = FakeDocker([container(cwd=str(tmp_path))])
    execute(context(tmp_path), 'update_latest', runner=docker, verify_timeout=0)
    deploy = next(cmd for cmd in docker.commands if 'up' in cmd)
    assert str(override) in deploy

@pytest.mark.parametrize('action', ['update_latest', 'deploy_version'])
def test_local_build_plan_checks_identity_before_any_build(action, tmp_path):
    class Versions:
        def get_git_tag_version_info(self, *args, **kwargs):
            from app.models.assets import PackageVersionInfo
            return PackageVersionInfo(latest_version='v2')
    adapter = DockerComposeAdapter(
        project_dir=str(tmp_path), compose_file='compose.yml', primary_container='demo', compose_service='web',
        lifecycle_strategy='compose_local_build_git_tag', recipe_repo_dir=str(tmp_path),
        override_file='override.yml', local_image_repository='demo', local_image_tag_template='{version}',
        runtime_context=context(tmp_path), version_service=Versions(),
    )
    plan = adapter.plan_action(action, version='v2')
    assert 'app.services.docker_lifecycle' in plan.commands[0]
    assert 'check' in plan.commands[0]
    # A changed container must fail the same preflight used by the plan.
    from app.services.docker_lifecycle import execute
    captured = json.loads(plan.commands[0][3])
    captured['containers'] = [{'name': 'demo', 'id': 'a' * 64}]
    docker = FakeDocker([container(id='b' * 64, cwd=str(tmp_path))])
    with pytest.raises(ValueError, match='身份'):
        execute(captured, 'check', runner=docker, verify_timeout=0)
    assert all(cmd[:3] in (['docker', 'ps', '-aq'], ['docker', 'container', 'inspect']) for cmd in docker.commands)


def test_identity_check_is_read_only(tmp_path):
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container(cwd=str(tmp_path))])
    execute(context(tmp_path), 'check', runner=docker, verify_timeout=0)
    assert all(cmd[:3] in (['docker', 'ps', '-aq'], ['docker', 'container', 'inspect']) for cmd in docker.commands)


@pytest.mark.parametrize('action', ['autostart_enable', 'autostart_disable'])
def test_autostart_uses_inspected_identity(action):
    adapter = DockerComposeAdapter(project_dir='/gone', compose_file='compose.yml', primary_container='demo', compose_service='web', runtime_context=context())
    plan = adapter.plan_action(action)
    assert 'app.services.docker_lifecycle' in plan.commands[0]


def test_local_build_preserves_compose_files_env_and_project_dir(tmp_path):
    ctx = context(tmp_path)
    ctx.update(compose_files=['base.yml', 'extra.yml'], env_files=['runtime.env'])
    adapter = DockerComposeAdapter(project_dir=str(tmp_path), compose_file='base.yml', primary_container='demo',
        compose_service='web', lifecycle_strategy='compose_local_build_git_tag', recipe_repo_dir=str(tmp_path / 'source'),
        override_file=str(tmp_path / 'override.yml'), local_image_repository='demo', local_image_tag_template='{version}',
        runtime_context=ctx)
    plan = adapter.plan_action('deploy_version', 'v2')
    deploy = next(c for c in plan.commands if 'compose' in c and 'up' in c)
    assert str(tmp_path / 'extra.yml') in deploy
    assert str(tmp_path / 'runtime.env') in deploy
    assert deploy[deploy.index('--project-directory') + 1] == str(tmp_path)
    assert deploy[deploy.index('-p') + 1] == 'original'
    assert plan.commands[plan.commands.index(deploy) - 1][-1] == 'check'
    assert any('config' in c and '--quiet' in c for c in plan.commands)


@pytest.mark.parametrize('action,policy', [('autostart_enable','unless-stopped'), ('autostart_disable','no')])
def test_autostart_updates_and_verifies_exact_container(tmp_path, action, policy):
    from app.services.docker_lifecycle import execute
    docker = FakeDocker([container(cwd=str(tmp_path))])
    def runner(command, **kwargs):
        result = docker(command, **kwargs)
        if command[:3] == ['docker', 'update', '--restart']:
            assert command[-1] == 'a' * 64
            docker.containers[0]['HostConfig'] = {'RestartPolicy': {'Name': command[3]}}
        return result
    execute(context(tmp_path), action, runner=runner, verify_timeout=0)
    assert docker.containers[0]['HostConfig']['RestartPolicy']['Name'] == policy
