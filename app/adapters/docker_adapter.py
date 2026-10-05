from app.services.state_store import resolve_state_db_path
import json
from pathlib import Path
import subprocess
import sys
from typing import Literal

import yaml

from app.adapters.base import ActionPlan
from app.services.compose_target import ComposeTarget
from app.models.assets import PackageVersionInfo, RuntimeVersionInfo
from app.services.docker_versions import DockerVersionService, REMOTE_TAG_REF_NAMESPACE

DockerAction = Literal[
    'update_latest',
    'deploy_version',
    'delete',
    'full_delete',
    'start',
    'stop',
    'restart',
    'autostart_enable',
    'autostart_disable',
    'cf_create',
    'cf_refresh',
    'cf_disable',
    'notify_send',
]
_OVERRIDE_FILE = '.wsl-ops-panel.override.yml'
_OVERRIDE_DEPLOY_SCRIPT = r'''
from pathlib import Path
import subprocess
import sys
import json
import tempfile
import os

compose_file, service, image = sys.argv[2:5]
fd, name = tempfile.mkstemp(prefix='.wsl-ops-', suffix='.json', dir='.')
override = Path(name)
try:
    with os.fdopen(fd, 'w') as stream:
        json.dump({'services': {service: {'image': image}}}, stream)
    subprocess.run(["docker", "compose", "-f", compose_file, "-f", str(override), "up", "-d", "--no-build", service], check=True)
finally:
    override.unlink(missing_ok=True)
'''
_HEALTHCHECK_COMMAND = r'''
import sys
import time
import urllib.request

url = sys.argv[1]
deadline = time.time() + 60
last_error = ''
while time.time() < deadline:
    try:
        response = urllib.request.urlopen(url, timeout=5)
        if response.status == 200:
            sys.exit(0)
        last_error = f'status={response.status}'
    except Exception as exc:
        last_error = repr(exc)
    time.sleep(2)
print(last_error, file=sys.stderr)
sys.exit(1)
'''
_INJECT_DOCKERFILE_BUILD_ENV_SCRIPT = r'''
from pathlib import Path
import sys

dockerfile = Path(sys.argv[1])
args = sys.argv[2:]
keys = [arg for arg in args if not arg.startswith('--apt-mirror=')]
apt_mirror = next((arg.split('=', 1)[1].rstrip('/') for arg in args if arg.startswith('--apt-mirror=')), '')
text = dockerfile.read_text(encoding='utf-8')
marker = '# wsl-ops-panel build env\n'
insert = marker + ''.join(f'ARG {key}\nENV {key}=${{{key}}}\n' for key in keys)
if apt_mirror:
    text = text.replace('http://deb.debian.org/debian-security', apt_mirror + '/debian-security')
    text = text.replace('http://deb.debian.org/debian', apt_mirror + '/debian')
    text = text.replace('https://deb.debian.org/debian-security', apt_mirror + '/debian-security')
    text = text.replace('https://deb.debian.org/debian', apt_mirror + '/debian')
    text = text.replace('URIs: http://deb.debian.org/debian-security', 'URIs: ' + apt_mirror + '/debian-security')
    text = text.replace('URIs: http://deb.debian.org/debian', 'URIs: ' + apt_mirror + '/debian')
    text = text.replace('URIs: https://deb.debian.org/debian-security', 'URIs: ' + apt_mirror + '/debian-security')
    text = text.replace('URIs: https://deb.debian.org/debian', 'URIs: ' + apt_mirror + '/debian')
    text = text.replace('RUN apt-get update', 'RUN sed -i "s#http://deb.debian.org/debian-security#' + apt_mirror + '/debian-security#g; s#http://deb.debian.org/debian#' + apt_mirror + '/debian#g; s#https://deb.debian.org/debian-security#' + apt_mirror + '/debian-security#g; s#https://deb.debian.org/debian#' + apt_mirror + '/debian#g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || true\nRUN apt-get update', 1)
if marker not in text:
    text = text.replace('\n', '\n' + insert, 1)
dockerfile.write_text(text, encoding='utf-8')
'''

_FETCH_TAG_IF_MISSING_SCRIPT = r'''
import subprocess
import sys

repo_dir, version, target_ref = sys.argv[1:4]
source_ref = f'refs/tags/{version}'


def run(command):
    return subprocess.run(command, check=False, capture_output=True, text=True)


if run(['git', '-C', repo_dir, 'rev-parse', '--verify', f'{target_ref}^{{commit}}']).returncode == 0:
    sys.exit(0)
if run(['git', '-C', repo_dir, 'rev-parse', '--verify', f'{source_ref}^{{commit}}']).returncode == 0:
    subprocess.run(['git', '-C', repo_dir, 'update-ref', target_ref, source_ref], check=True)
    sys.exit(0)
completed = subprocess.run(
    [
        'timeout',
        '120',
        'git',
        '-C',
        repo_dir,
        '-c',
        'http.version=HTTP/1.1',
        'fetch',
        'origin',
        f'{source_ref}:{target_ref}',
    ],
    check=False,
)
sys.exit(completed.returncode)
'''


_TRANSIENT_NETWORK_RETRY_POLICY = {
    'max_attempts': 3,
    'delay_seconds': 5.0,
    'retry_on_stderr': [
        'EOF',
        'TLS handshake timeout',
        'connection reset by peer',
        'gnutls_handshake() failed',
        'The TLS connection was non-properly terminated',
        'ConnectionResetError',
    ],
}


class DockerComposeAdapter:
    def __init__(
        self,
        *,
        project_dir: str,
        compose_file: str,
        primary_container: str,
        compose_service: str,
        image_repository: str | None = None,
        current_version: str | None = None,
        source_url: str | None = None,
        lifecycle_strategy: str = 'compose_pull',
        override_file: str | None = None,
        recipe_repo_dir: str | None = None,
        local_image_repository: str | None = None,
        local_image_tag_template: str | None = None,
        build_worktree_dir: str | None = None,
        docker_build_args: dict[str, str] | None = None,
        healthcheck_url: str | None = None,
        runtime: RuntimeVersionInfo | None = None,
        managed_services: list[str] | None = None,
        ignored_services: list[str] | None = None,
        version_service: DockerVersionService | None = None,
        config_root: str = 'config',
        asset_snapshot_json: str | None = None,
        runtime_context: dict | None = None,
    ) -> None:
        self.runtime_context = runtime_context
        self.project_dir = project_dir
        self.compose_file = compose_file
        self.primary_container = primary_container
        self.compose_service = compose_service
        self.image_repository = image_repository
        self.current_version = current_version
        self.source_url = source_url
        self.lifecycle_strategy = lifecycle_strategy
        self.override_file = override_file
        self.recipe_repo_dir = recipe_repo_dir
        self.local_image_repository = local_image_repository
        self.local_image_tag_template = local_image_tag_template
        self.build_worktree_dir = build_worktree_dir
        self.docker_build_args = dict(docker_build_args or {})
        self.healthcheck_url = healthcheck_url
        self.runtime = runtime
        self.managed_services = list(managed_services or [])
        self.ignored_services = list(ignored_services or [])
        self.config_root = config_root
        self.asset_snapshot_json = asset_snapshot_json
        self.asset_snapshot_id = _extract_asset_snapshot_id(asset_snapshot_json)
        self._version_service = version_service or DockerVersionService()

    def plan_action(self, action: DockerAction, version: str | None = None) -> ActionPlan:
        runtime_actions = {'start', 'stop', 'restart', 'delete', 'full_delete', 'autostart_enable', 'autostart_disable'}
        if self.lifecycle_strategy == 'compose_pull':
            runtime_actions |= {'update_latest', 'deploy_version'}
        if self.runtime_context is not None and action in runtime_actions:
            context = self._runtime_context()
            command = self._runtime_command(action, version)
            return ActionPlan(
                commands=[command],
                working_dir=str(Path(__file__).resolve().parents[2]),
                command_timeout_seconds=1800,
                success_commands=[_deleted_asset_command(self.asset_snapshot_id, self.config_root)] if action == 'full_delete' else [],
                preview_objects=([f'{self.image_repository}:{version}'] if action == 'deploy_version'
                                 else [item for item in [self.compose_service, self.primary_container, self.image_repository] if item]),
                preview_paths=[self.project_dir, *[str(Path(self.project_dir) / file) for file in context['compose_files']]],
            )
        if self.lifecycle_strategy == 'compose_pull':
            return self._plan_compose_pull(action, version)
        if self.lifecycle_strategy == 'compose_local_build_git_tag':
            plan = self._plan_compose_local_build_git_tag(action, version)
            if self.runtime_context is not None and action in {'update_latest', 'deploy_version'}:
                check = self._runtime_command('check')
                commands = [check]
                for command in plan.commands:
                    if 'compose' in command and 'up' in command:
                        commands.append(check.copy())
                    commands.append(command)
                plan = plan.model_copy(update={'commands': commands})
            return plan
        raise ValueError(f'unsupported lifecycle strategy: {self.lifecycle_strategy}')


    def _runtime_context(self) -> dict:
        context = {
            'project_dir': self.project_dir, 'primary_container': self.primary_container,
            'compose_service': self.compose_service, 'image_repository': self.image_repository,
            'managed_services': self.managed_services, 'ignored_services': self.ignored_services,
            'compose_file': self.compose_file, **(self.runtime_context or {}),
        }
        target = ComposeTarget.from_context(context)
        context.update(compose_files=list(target.files), env_files=list(target.env_files))
        return context

    def _runtime_command(self, action: str, version: str | None = None) -> list[str]:
        command = [sys.executable, '-m', 'app.services.docker_lifecycle',
                   json.dumps(self._runtime_context(), ensure_ascii=False), action]
        return [*command, version] if version else command

    def _plan_common_action(self, action: DockerAction) -> ActionPlan | None:
        if action == 'start':
            return ActionPlan(
                commands=[['docker', 'compose', '-f', self.compose_file, 'up', '-d']],
                working_dir=self.project_dir,
                preview_objects=['full compose stack'],
            )
        if action == 'stop':
            return ActionPlan(
                commands=[['docker', 'compose', '-f', self.compose_file, 'stop']],
                working_dir=self.project_dir,
                preview_objects=['full compose stack'],
            )
        if action == 'restart':
            return ActionPlan(
                commands=[['docker', 'compose', '-f', self.compose_file, 'restart']],
                working_dir=self.project_dir,
                preview_objects=['full compose stack'],
            )
        if action == 'autostart_enable':
            return ActionPlan(
                commands=[['docker', 'update', '--restart', 'unless-stopped', self.primary_container]],
                preview_objects=[self.primary_container, 'unless-stopped'],
            )
        if action == 'autostart_disable':
            return ActionPlan(
                commands=[['docker', 'update', '--restart', 'no', self.primary_container]],
                preview_objects=[self.primary_container, 'no'],
            )
        if action in {'cf_create', 'cf_refresh'}:
            unit_name = _guess_cftunnel_unit(self.project_dir)
            if unit_name:
                return ActionPlan(
                    commands=[['sudo', 'systemctl', 'restart', unit_name]],
                    requires_sudo=True,
                    preview_objects=[unit_name],
                )
            script_path = str(Path(self.project_dir) / 'scripts' / 'cftunnel-start.sh')
            return ActionPlan(
                commands=[['bash', script_path]],
                working_dir=self.project_dir,
                preview_paths=[script_path],
            )
        if action == 'cf_disable':
            unit_name = _guess_cftunnel_unit(self.project_dir)
            if not unit_name:
                raise ValueError('cf_disable requires a detected cftunnel systemd unit')
            return ActionPlan(
                commands=[['sudo', 'systemctl', 'disable', '--now', unit_name]],
                requires_sudo=True,
                preview_objects=[unit_name],
            )
        if action == 'notify_send':
            if not self.asset_snapshot_json:
                raise ValueError('notify_send requires asset snapshot json')
            return ActionPlan(
                commands=[[
                    sys.executable,
                    '-m',
                    'app.services.notifications',
                    'send',
                    '--config-root',
                    self.config_root,
                    '--asset-id',
                    self.asset_snapshot_id,
                    '--asset-json',
                    self.asset_snapshot_json,
                ]],
                working_dir=str(Path(__file__).resolve().parents[2]),
                preview_objects=[self.asset_snapshot_id],
            )
        return None

    def get_version_info(self) -> PackageVersionInfo:
        if self.lifecycle_strategy == 'compose_local_build_git_tag':
            if not self.recipe_repo_dir:
                raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
            info = self._version_service.get_git_tag_version_info(self.recipe_repo_dir, remote=True)
        else:
            if not self.image_repository:
                return PackageVersionInfo(
                    current_version=self.current_version,
                    lifecycle_strategy=self.lifecycle_strategy,
                    version_source='registry_tags',
                    runtime=self.runtime,
                    managed_services=self.managed_services,
                    ignored_services=self.ignored_services,
                )
            registry_kwargs = {'current_version': self.current_version}
            if self.source_url:
                registry_kwargs['source_url'] = self.source_url
            info = self._version_service.get_registry_tag_version_info(
                self.image_repository,
                **registry_kwargs,
            )

        default_source = 'git_tags' if self.lifecycle_strategy == 'compose_local_build_git_tag' else 'registry_tags'
        update_payload = {
            'lifecycle_strategy': self.lifecycle_strategy,
            'version_source': info.version_source or default_source,
            'runtime': self.runtime,
            'managed_services': self.managed_services,
            'ignored_services': self.ignored_services,
        }
        if self.lifecycle_strategy == 'compose_local_build_git_tag' and not info.current_version:
            runtime_version = self._runtime_current_git_version()
            if runtime_version:
                update_payload['current_version'] = runtime_version
        return info.model_copy(update=update_payload)

    def _plan_compose_pull(self, action: DockerAction, version: str | None) -> ActionPlan:
        common_plan = self._plan_common_action(action)
        if common_plan is not None:
            return common_plan

        if action == 'update_latest':
            return ActionPlan(
                commands=[
                    ['docker', 'compose', '-f', self.compose_file, 'pull', '--ignore-buildable', self.compose_service],
                    ['docker', 'compose', '-f', self.compose_file, 'up', '-d', '--no-build', self.compose_service],
                ],
                retry_policy=_TRANSIENT_NETWORK_RETRY_POLICY.copy(),
                working_dir=self.project_dir,
            )

        if action == 'deploy_version':
            if not version:
                raise ValueError('deploy_version requires a target version')
            if not self.image_repository:
                raise ValueError('deploy_version requires a detected image repository')

            image_ref = f'{self.image_repository}:{version}'
            return ActionPlan(
                commands=[
                    [
                        'python3',
                        '-c',
                        _OVERRIDE_DEPLOY_SCRIPT,
                        _OVERRIDE_FILE,
                        self.compose_file,
                        self.compose_service,
                        image_ref,
                    ],
                ],
                working_dir=self.project_dir,
                preview_objects=[image_ref],
            )

        if action == 'delete':
            return ActionPlan(
                commands=[['docker', 'compose', '-f', self.compose_file, 'rm', '-f', '-s', self.compose_service]],
                working_dir=self.project_dir,
            )

        if action == 'full_delete':
            compose_path = str(Path(self.project_dir) / self.compose_file)
            preview_objects = [self.compose_service, self.primary_container]
            if self.image_repository:
                preview_objects.append(self.image_repository)
            return ActionPlan(
                commands=[
                    ['docker', 'compose', '-f', self.compose_file, 'down', '--remove-orphans', '--rmi', 'all', '--volumes'],
                    ['rm', '-rf', self.project_dir],
                ],
                working_dir=self.project_dir,
                success_commands=[_deleted_asset_command(self.asset_snapshot_id, self.config_root)],
                preview_paths=[self.project_dir, compose_path],
                preview_objects=_unique_preserving_order(preview_objects),
            )

        raise ValueError(f'unsupported docker action: {action}')

    def _plan_compose_local_build_git_tag(self, action: DockerAction, version: str | None) -> ActionPlan:
        common_plan = self._plan_common_action(action)
        if common_plan is not None:
            return common_plan
        if action in {'delete', 'full_delete'}:
            return self._plan_compose_pull(action, version)
        if action == 'update_latest':
            version = self._resolve_latest_git_tag()
            if self._runtime_matches_local_git_tag(version):
                commands = []
                if self.healthcheck_url:
                    commands.append(['python3', '-c', _HEALTHCHECK_COMMAND, self.healthcheck_url])
                return ActionPlan(
                    commands=commands or [['true']],
                    retry_policy=_TRANSIENT_NETWORK_RETRY_POLICY.copy(),
                    working_dir=self.recipe_repo_dir,
                    preview_objects=['already latest', version],
                )
        if version is None:
            raise ValueError('compose_local_build_git_tag requires a resolved git tag')
        if not self.recipe_repo_dir:
            raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
        if not self.override_file:
            raise ValueError('compose_local_build_git_tag requires override_file')
        if not self.local_image_repository:
            raise ValueError('compose_local_build_git_tag requires local_image_repository')
        if not self.local_image_tag_template:
            raise ValueError('compose_local_build_git_tag requires local_image_tag_template')

        image_ref = f'{self.local_image_repository}:{self.local_image_tag_template.format(version=version)}'
        source_dir = self._build_source_dir(version)
        dockerfile = str(Path(source_dir) / 'Dockerfile') if self.build_worktree_dir else 'Dockerfile'
        build_context = source_dir if self.build_worktree_dir else '.'
        build_command = ['docker', 'build', '-t', image_ref]
        build_command.extend(_format_image_labels(version, self.recipe_repo_dir))
        build_command.extend(_format_docker_build_args(self.docker_build_args, version=version))
        build_command.extend(['-f', dockerfile, build_context])
        compose_file = str(Path(self.project_dir) / self.compose_file) if self.build_worktree_dir else self.compose_file
        commands = [
            *self._prepare_build_source_commands(version, source_dir),
            *_prepare_build_env_commands(
                dockerfile=dockerfile,
                build_args=self.docker_build_args,
                enabled=bool(self.build_worktree_dir),
            ),
            build_command,
            [
                'env',
                f'WSL_OPS_IMAGE={image_ref}',
                'docker',
                'compose',
                '-f',
                compose_file,
                '-f',
                self.override_file,
                'up',
                '-d',
                '--no-build',
                self.compose_service,
            ],
        ]
        if self.runtime_context is not None:
            target = ComposeTarget.from_context(self._runtime_context())
            compose = ['env', f'WSL_OPS_IMAGE={image_ref}', *target.command(extra_files=[self.override_file])]
            commands[-1] = [*compose, 'up', '-d', '--no-build', self.compose_service]
            commands.insert(0, [*compose, 'config', '--quiet'])
        if self.healthcheck_url:
            commands.append(['python3', '-c', _HEALTHCHECK_COMMAND, self.healthcheck_url])

        return ActionPlan(
            commands=commands,
            retry_policy=_TRANSIENT_NETWORK_RETRY_POLICY.copy(),
            command_timeout_seconds=900,
            working_dir=self.recipe_repo_dir,
            preview_objects=[image_ref, version],
        )


    def _build_source_dir(self, version: str) -> str:
        if not self.build_worktree_dir:
            if not self.recipe_repo_dir:
                raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
            return self.recipe_repo_dir
        return str(Path(self.build_worktree_dir) / _safe_path_name(version))

    def _prepare_build_source_commands(self, version: str, source_dir: str) -> list[list[str]]:
        if not self.build_worktree_dir:
            if not self.recipe_repo_dir:
                raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
            return [['git', '-C', self.recipe_repo_dir, 'checkout', version]]
        if not self.recipe_repo_dir:
            raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
        return [
            ['rm', '-rf', source_dir],
            [
                'python3',
                '-c',
                _FETCH_TAG_IF_MISSING_SCRIPT,
                self.recipe_repo_dir,
                version,
                f'refs/tags/{REMOTE_TAG_REF_NAMESPACE}/{version}',
            ],
            [
                'git',
                '-C',
                self.recipe_repo_dir,
                'worktree',
                'add',
                '--force',
                '--detach',
                source_dir,
                f'refs/tags/{REMOTE_TAG_REF_NAMESPACE}/{version}',
            ],
        ]


    def _runtime_matches_local_git_tag(self, version: str) -> bool:
        runtime_version = self._runtime_current_git_version()
        if runtime_version == version:
            return True
        if not self.current_version or not self.local_image_tag_template:
            return False
        normalized_version = version.removeprefix('v')
        expected_tag = self.local_image_tag_template.format(version=version)
        return self.current_version in {version, expected_tag, normalized_version}

    def _runtime_current_git_version(self) -> str | None:
        if self.runtime is not None and self.runtime.oci_version:
            return self.runtime.oci_version if self.runtime.oci_version.startswith('v') else f'v{self.runtime.oci_version}'
        return None

    def _resolve_latest_git_tag(self) -> str:
        if not self.recipe_repo_dir:
            raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
        info = self._version_service.get_git_tag_version_info(self.recipe_repo_dir, remote=True)
        if not info.latest_version:
            raise ValueError('compose_local_build_git_tag could not resolve latest git tag')
        return info.latest_version

    def list_available_versions(self) -> list[str]:
        versions: list[str] = []
        if self.current_version:
            versions.append(self.current_version)
        if self.current_version != 'latest':
            versions.append('latest')

        if self.image_repository:
            try:
                completed = subprocess.run(
                    ['docker', 'image', 'ls', self.image_repository, '--format', '{{json .}}'],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                completed = None

            if completed is not None:
                for line in completed.stdout.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        row = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    tag = row.get('Tag')
                    if not tag or tag == '<none>':
                        continue
                    versions.append(tag)

        return _unique_preserving_order(versions)


def detect_image_repository_from_compose(project_dir: str, compose_file: str, compose_service: str) -> str | None:
    compose_path = Path(project_dir) / compose_file
    if not compose_path.exists():
        return None

    try:
        payload = yaml.safe_load(compose_path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(payload, dict):
        return None

    services = payload.get('services')
    if not isinstance(services, dict):
        return None
    service_config = services.get(compose_service)
    if not isinstance(service_config, dict):
        return None

    image_value = _resolve_compose_image_value(service_config.get('image'))
    return parse_image_repository(image_value)


def parse_image_repository(image: str | None) -> str | None:
    if not image:
        return None
    if '@' in image:
        image = image.split('@', 1)[0]
    if ':' not in image:
        return image
    last_slash = image.rfind('/')
    last_colon = image.rfind(':')
    if last_colon <= last_slash:
        return image
    return image[:last_colon]


def _resolve_compose_image_value(image: object) -> str | None:
    if not isinstance(image, str):
        return None
    if image.startswith('${') and image.endswith('}'):
        inner = image[2:-1]
        for marker in (':-', '-'):
            if marker in inner:
                return inner.split(marker, 1)[1]
        return None
    return image




def _format_docker_build_args(build_args: dict[str, str], *, version: str) -> list[str]:
    formatted: list[str] = []
    context = {
        'version': version,
        'version_without_v': version.removeprefix('v'),
        'docker_bridge_proxy_url': 'http://172.17.0.1:7890',
    }
    for key, template in build_args.items():
        formatted.extend(['--build-arg', f'{key}={template.format(**context)}'])
    return formatted


def _format_image_labels(version: str, repo_dir: str | None) -> list[str]:
    labels = ['--label', f'org.opencontainers.image.version={version.removeprefix("v")}']
    if repo_dir and Path(repo_dir).name == 'openai-cpa':
        labels.extend(['--label', 'org.opencontainers.image.source=https://github.com/wenfxl/openai-cpa'])
    return labels


def _prepare_build_env_commands(*, dockerfile: str, build_args: dict[str, str], enabled: bool) -> list[list[str]]:
    if not enabled:
        return []
    env_keys = [key for key in build_args if key.startswith(('PIP_', 'HTTP_', 'HTTPS_', 'http_', 'https_'))]
    extra_args: list[str] = []
    apt_mirror = build_args.get('APT_MIRROR') or build_args.get('DEBIAN_MIRROR')
    if apt_mirror:
        extra_args.append(f'--apt-mirror={apt_mirror}')
    if not env_keys and not extra_args:
        return []
    return [['python3', '-c', _INJECT_DOCKERFILE_BUILD_ENV_SCRIPT, dockerfile, *env_keys, *extra_args]]


def _safe_path_name(value: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in {'.', '_', '-'} else '-' for ch in value) or 'version'


def _deleted_asset_command(asset_id: str, config_root: str = 'config') -> list[str]:
    return [
        sys.executable,
        '-c',
        (
            'from pathlib import Path; import sys, yaml; '
            'path = Path(sys.argv[1]); asset_id = sys.argv[2]; '
            'path.parent.mkdir(parents=True, exist_ok=True); '
            'payload = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}; '
            'items = payload.get("assets", []) if isinstance(payload, dict) else []; '
            'items = [str(item) for item in items if str(item)]; '
            'items.append(asset_id) if asset_id and asset_id not in items else None; '
            'path.write_text(yaml.safe_dump({"assets": sorted(items)}, allow_unicode=True, sort_keys=False), encoding="utf-8")'
        ),
        str(resolve_state_db_path(Path(config_root).resolve()).parent / 'deleted_assets.yaml'),
        asset_id,
    ]

def _unique_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _guess_cftunnel_unit(project_dir: str) -> str | None:
    name = Path(project_dir).name
    candidates = [
        f'{name}-cftunnel.service',
        f'{name.replace("_", "-")}-cftunnel.service',
    ]
    aliases = {
        'CLIProxyAPI': 'cftunnel.service',
        'new-api': 'newapi-cftunnel.service',
        'searxng-mcp': 'searxng-cftunnel.service',
    }
    if name in aliases:
        candidates.insert(0, aliases[name])
    for candidate in candidates:
        if Path('/etc/systemd/system', candidate).exists():
            return candidate
    return None


def _extract_asset_snapshot_id(asset_snapshot_json: str | None) -> str:
    if not asset_snapshot_json:
        return ''
    try:
        payload = json.loads(asset_snapshot_json)
    except json.JSONDecodeError:
        return ''
    value = payload.get('object_id') if isinstance(payload, dict) else None
    return value if isinstance(value, str) else ''
