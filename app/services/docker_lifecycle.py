"""Execute panel Docker actions against inspected identities, not guessed directories.

Container environment is deliberately never reused as Compose interpolation input.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

PREFIX = 'com.docker.compose.'


def _labels(container):
    return container.get('Config', {}).get('Labels') or {}


def execute(context, action, *, version=None, runner=subprocess.run, verify_timeout=60):
    def run(command, *, cwd=None, quiet=False):
        pulling = command[:2] == ['docker', 'compose'] and 'pull' in command
        attempts = 3 if pulling else 1
        for attempt in range(attempts):
            result = runner(command, cwd=cwd, capture_output=True, text=True, timeout=300 if pulling else 900)
            transient = any(marker in (result.stderr or '').lower() for marker in
                            ('eof', 'tls handshake timeout', 'connection reset', 'temporary failure', 'i/o timeout'))
            if not result.returncode or not transient or attempt + 1 == attempts:
                break
            print(f'镜像网络临时异常，重试 {attempt + 2}/{attempts}', flush=True)
            time.sleep(2)
        if not quiet:
            if result.stdout:
                print(result.stdout, end='', flush=True)
            if result.stderr:
                print(result.stderr, end='', file=sys.stderr, flush=True)
        if result.returncode:
            # Inspect output may contain secrets: never include it in diagnostics.
            raise RuntimeError(f'{" ".join(command[:3])} 执行失败 (exit={result.returncode})')
        return result.stdout

    def inspect_all():
        ids = run(['docker', 'ps', '-aq', '--no-trunc'], quiet=True).split()
        return json.loads(run(['docker', 'container', 'inspect', *ids], quiet=True)) if ids else []

    all_containers = inspect_all()
    primary_name = context['primary_container']
    primary = next((c for c in all_containers if c['Name'].lstrip('/') == primary_name), None)
    project = context.get('project') or (_labels(primary).get(PREFIX + 'project') if primary else None)
    if primary and project and _labels(primary).get(PREFIX + 'project') != project:
        raise ValueError('容器项目归属已变化，请刷新后重试')
    project_dir = Path(context['project_dir']).absolute()
    ignored = set(context.get('ignored_services', []))

    def selected(containers):
        if not project:
            return [c for c in containers if c['Name'].lstrip('/') == primary_name]
        return [c for c in containers if _labels(c).get(PREFIX + 'project') == project
                and _labels(c).get(PREFIX + 'service') not in ignored
                and _labels(c).get(PREFIX + 'oneoff', '').lower() != 'true']

    targets = selected(all_containers)
    expected = {c['name']: c['id'] for c in context.get('containers', [])}
    for c in targets:
        old_id = expected.get(c['Name'].lstrip('/'))
        if expected and (not old_id or not c['Id'].startswith(old_id)):
            raise ValueError('容器身份已变化，请刷新后重试')
        cwd = _labels(c).get(PREFIX + 'project.working_dir')
        if cwd and Path(cwd).absolute() != project_dir:
            raise ValueError('项目工作路径与容器归属不一致，请刷新后重试')

    if action == 'check':
        print('执行目标身份校验通过', flush=True)
        return
    if action in {'autostart_enable', 'autostart_disable'}:
        if primary is None or primary not in targets:
            raise ValueError('原容器已不存在，请刷新后重试')
        policy = 'unless-stopped' if action == 'autostart_enable' else 'no'
        run(['docker', 'update', '--restart', policy, primary['Id']])
        current = next((c for c in inspect_all() if c['Id'] == primary['Id']), None)
        if current is None or current.get('HostConfig', {}).get('RestartPolicy', {}).get('Name') != policy:
            raise RuntimeError('容器重启策略核验失败')
        print('容器重启策略核验通过', flush=True)
        return

    completed_services = set()
    dependency_waits = set()
    for c in targets:
        for dep in _labels(c).get(PREFIX + 'depends_on', '').split(','):
            parts = dep.split(':')
            if len(parts) > 1 and parts[1] in {'service_healthy', 'service_completed_successfully'}:
                dependency_waits.add(parts[0])
            if len(parts) > 1 and parts[1] == 'service_completed_successfully':
                completed_services.add(parts[0])

    def state_matches(c, wanted):
        state = c['State']
        if wanted and _labels(c).get(PREFIX + 'service') in completed_services:
            return not state.get('Running') and state.get('Status') == 'exited' and state.get('ExitCode') == 0
        return (bool(state.get('Running')) == wanted and not any(state.get(k) for k in ('Restarting', 'Paused', 'Dead'))
                and (not wanted or state.get('Health', {}).get('Status', 'healthy') == 'healthy'))

    def verify_running(wanted, ids=None, services=None):
        deadline = time.monotonic() + verify_timeout
        stable_since = None
        stable_identity = None
        while True:
            current = selected(inspect_all())
            if ids is not None:
                current = [c for c in current if c['Id'] in ids]
            if services:
                current = [c for c in current if _labels(c).get(PREFIX + 'service') in services]
            present = bool(current) if wanted else True
            if ids is not None:
                present = len(current) == len(ids)
            if services:
                present = set(services) <= {_labels(c).get(PREFIX + 'service') for c in current}
            healthy = present and all(state_matches(c, wanted) for c in current)
            identity = [(c['Id'], c['State'].get('StartedAt'), c.get('RestartCount')) for c in current]
            if healthy:
                if identity != stable_identity:
                    stable_since = time.monotonic()
                    stable_identity = identity
                if not wanted or verify_timeout == 0 or time.monotonic() - stable_since >= 2:
                    print('实际容器状态核验通过', flush=True)
                    return
            else:
                stable_since = None
                stable_identity = None
            if time.monotonic() >= deadline:
                raise RuntimeError('容器状态核验失败：服务未达到目标状态，请查看容器日志')
            time.sleep(1)

    has_application = any(_labels(c).get(PREFIX + 'service') == context['compose_service'] for c in targets)
    if action in {'start', 'stop', 'restart'} and targets and (action != 'start' or has_application):
        # Compose dependency labels survive even when deployment files have gone.
        by_service = {_labels(c).get(PREFIX + 'service'): c for c in targets}
        ordered, visited = [], set()
        def visit(c):
            if c['Id'] in visited:
                return
            visited.add(c['Id'])
            for dep in _labels(c).get(PREFIX + 'depends_on', '').split(','):
                dependency = by_service.get(dep.split(':')[0])
                if dependency:
                    visit(dependency)
            ordered.append(c)
        for c in targets:
            visit(c)
        if action == 'stop':
            ordered.reverse()
        for c in ordered:
            # Starting a running container is a no-op; never recreate it.
            if not (action == 'start' and (c['State'].get('Running') or
                    (_labels(c).get(PREFIX + 'service') in completed_services and state_matches(c, True)))):
                run(['docker', action, c['Id']])
            if action != 'stop' and _labels(c).get(PREFIX + 'service') in dependency_waits:
                verify_running(True, ids={c['Id']})
        verify_running(action != 'stop', ids={c['Id'] for c in targets})
        return
    if action in {'stop', 'restart'}:
        if action == 'restart':
            raise ValueError('原容器已不存在，请使用启动进行首次部署')
        print('容器已停止或已删除')
        return

    if action in {'delete', 'full_delete'}:
        if action == 'delete':
            targets = [c for c in targets if _labels(c).get(PREFIX + 'service') == context['compose_service']]
        target_ids = {c['Id'] for c in targets}
        others = [c for c in all_containers if c['Id'] not in target_ids]
        shared_dir = any(
            (_labels(c).get(PREFIX + 'project.working_dir') and _paths_overlap(project_dir, Path(_labels(c)[PREFIX + 'project.working_dir'])))
            or any(m.get('Type') == 'bind' and _paths_overlap(project_dir, Path(m.get('Source', '/'))) for m in c.get('Mounts', []))
            for c in others)
        removal = None
        if action == 'full_delete' and not shared_dir and (targets or expected):
            removal = _directory_removal(project_dir, runner)
        elif action == 'full_delete':
            print('保留项目目录（共享目录或缺少容器归属证据）', flush=True)
        # Preflight above happens before the first destructive command.
        anonymous = {m['Name'] for c in targets for m in c.get('Mounts', [])
                     if m.get('Type') == 'volume' and len(m.get('Name', '')) == 64
                     and all(ch in '0123456789abcdef' for ch in m['Name'])}
        for c in targets:
            run(['docker', 'rm', '-f', c['Id']])
        if action == 'full_delete' and project:
            for resource in ('network', 'volume'):
                names = set(run(['docker', resource, 'ls', '-q', '--filter', f'label={PREFIX}project={project}'], quiet=True).split())
                if resource == 'volume':
                    names |= anonymous
                for name in sorted(names):
                    data = json.loads(run(['docker', resource, 'inspect', name], quiet=True))[0]
                    current = inspect_all()
                    if resource == 'volume':
                        in_use = any(m.get('Name') == name for c in current for m in c.get('Mounts', []))
                    else:
                        in_use = bool(data.get('Containers')) or any(name in c.get('NetworkSettings', {}).get('Networks', {}) for c in current)
                    if in_use:
                        print(f'保留共享{resource}: {name}', flush=True)
                        continue
                    run(['docker', resource, 'rm', name])
        # Images may be shared by non-running deployments: retain rather than --rmi all.
        if removal:
            if removal[0] == 'sudo':
                run(removal)
            elif project_dir.exists():
                shutil.rmtree(project_dir)
            if project_dir.exists():
                raise RuntimeError('删除后项目目录仍存在')
        if target_ids & {c['Id'] for c in inspect_all()}:
            raise RuntimeError('删除后仍有目标容器残留')
        print('删除完成，已核验目标容器和目录；共享资源及镜像保留', flush=True)
        return

    # Deployment path: preserve explicitly supplied context, then validate before pull/up.
    files = context.get('compose_files') or ['docker-compose.yml']
    if not project_dir.is_dir():
        raise ValueError(f'Compose 工作目录不存在：{project_dir}；启停原容器不依赖此目录')
    command = ['docker', 'compose']
    if project:
        command += ['-p', project]
    for file in files:
        path = Path(file)
        path = path if path.is_absolute() else project_dir / path
        if not path.is_file():
            raise ValueError(f'Compose 文件不存在：{path}')
        command += ['-f', str(path)]
    for file in context.get('env_files', []):
        path = Path(file)
        path = path if path.is_absolute() else project_dir / path
        if not path.is_file():
            raise ValueError(f'Compose 环境文件不存在：{path}')
        command += ['--env-file', str(path)]
    persistent_override = project_dir / '.wsl-ops-panel.version.json'
    if persistent_override.exists() and str(persistent_override) not in command:
        if persistent_override.is_symlink():
            raise ValueError('版本覆盖文件路径是软链接')
        command += ['-f', str(persistent_override)]
    services = context.get('managed_services') or [context['compose_service']]
    services = [s for s in services if s not in ignored]
    if not services:
        raise ValueError('没有可部署的服务')
    override = None
    previous_override = None
    deployed = False
    try:
        if action == 'deploy_version':
            if not version or not context.get('image_repository'):
                raise ValueError('指定版本部署缺少版本或镜像仓库')
            override = project_dir / '.wsl-ops-panel.version.json'
            if override.is_symlink():
                raise ValueError('版本覆盖文件路径是软链接')
            previous_override = override.read_bytes() if override.exists() else b'{"services":{}}'
            _atomic_write(override, json.dumps({'services': {context['compose_service']: {'image': f"{context['image_repository']}:{version}"}}}).encode())
            if str(override) not in command:
                command += ['-f', str(override)]
        run([*command, 'config', '--quiet'], cwd=str(project_dir))
        if action in {'update_latest', 'deploy_version'}:
            run([*command, 'pull', '--ignore-buildable', *services], cwd=str(project_dir))
        elif action != 'start':
            raise ValueError(f'unsupported action: {action}')
        run([*command, 'up', '-d', '--no-build', *services], cwd=str(project_dir))
        if not project:
            created_ids = run([*command, 'ps', '-aq'], cwd=str(project_dir), quiet=True).split()
            created = [c for c in inspect_all() if any(c['Id'].startswith(i) for i in created_ids)]
            if created:
                project = _labels(created[0]).get(PREFIX + 'project')
        verify_running(True, services=services)
        deployed = True
    finally:
        if override and not deployed and previous_override is not None:
            # Keep the path valid for labels even after a partial deployment.
            _atomic_write(override, previous_override)
            print('部署未完成；已恢复版本覆盖配置。请检查实际容器状态，数据未自动回滚。', file=sys.stderr)



def _atomic_write(path: Path, content: bytes):
    fd, name = tempfile.mkstemp(prefix='.wsl-ops-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _paths_overlap(a: Path, b: Path):
    a, b = a.resolve(), b.resolve()
    return a == b or a in b.parents or b in a.parents


def _directory_removal(path: Path, runner):
    # Never follow a symlink or erase a broad root, home, panel, or mount point.
    protected = [Path.home(), Path(__file__).resolve().parents[2]]
    if len(path.parts) < 4 or path.resolve() != path or any(path == p or path in p.parents for p in protected) or path.is_mount():
        raise ValueError(f'删除路径范围异常：{path}')
    if not path.exists():
        return None
    if not path.is_dir():
        raise ValueError(f'删除路径不是目录：{path}')
    writable = os.access(path.parent, os.W_OK | os.X_OK)
    def unreadable(_error):
        nonlocal writable
        writable = False
    for root, dirs, _ in os.walk(path, followlinks=False, onerror=unreadable):
        if os.path.ismount(root) and Path(root) != path:
            raise ValueError(f'删除路径含嵌套挂载：{root}')
        writable &= os.access(root, os.R_OK | os.W_OK | os.X_OK)
    if writable:
        return ['rmtree', str(path)]
    rm = shutil.which('rm') or '/usr/bin/rm'
    command = [rm, '-rf', '--one-file-system', '--', str(path)]
    probe = runner(['sudo', '-n', '-l', '--', *command], capture_output=True, text=True, timeout=10)
    if probe.returncode:
        raise ValueError(f'目录权限预检失败：{path}；尚未删除容器，请配置此路径的限定清理权限')
    return ['sudo', '-n', *command]


def main():
    try:
        execute(json.loads(sys.argv[1]), sys.argv[2], version=sys.argv[3] if len(sys.argv) > 3 else None)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
