from pathlib import Path
import json
import sys
from typing import Literal

from app.adapters.base import ActionPlan
from app.models.assets import PackageVersionInfo
from app.services.docker_versions import DockerVersionService
from app.services.project_lifecycle import unit_groups

ProjectAction = Literal[
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


class ProjectAdapter:
    def __init__(
        self,
        *,
        project_dir: str,
        current_version: str | None = None,
        git_remote_url: str | None = None,
        service_unit: str | None = None,
        service_units: list[str] | None = None,
        service_scope: str = 'system',
        service_targets: list[dict[str, str]] | None = None,
        version_service: DockerVersionService | None = None,
        cftunnel_unit: str | None = None,
        cftunnel_script: str | None = None,
        config_root: str = 'config',
        asset_snapshot_json: str | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.current_version = current_version
        self.git_remote_url = git_remote_url
        self.service_unit = service_unit
        self.service_units = list(dict.fromkeys(service_units or ([service_unit] if service_unit else [])))
        self.service_scope = service_scope
        self.service_targets = service_targets or [dict(name=name, scope=service_scope) for name in self.service_units]
        self.service_units = list(dict.fromkeys(name for _, names in unit_groups(dict(service_targets=self.service_targets)) for name in names))
        self._version_service = version_service or DockerVersionService()
        self.cftunnel_unit = cftunnel_unit
        self.cftunnel_script = cftunnel_script
        self.config_root = config_root
        self.asset_snapshot_json = asset_snapshot_json
        self.asset_snapshot_id = _extract_asset_snapshot_id(asset_snapshot_json)

    def plan_action(self, action: ProjectAction, version: str | None = None) -> ActionPlan:
        common_plan = self._plan_common_action(action)
        if common_plan is not None:
            return common_plan
        if action == 'update_latest':
            return ActionPlan(
                commands=[['git', '-C', self.project_dir, 'pull', '--ff-only']],
                preview_paths=[self.project_dir],
            )
        if action == 'deploy_version':
            if not version:
                raise ValueError('deploy_version requires a target version')
            return ActionPlan(
                commands=[['git', '-C', self.project_dir, 'fetch', '--tags'], ['git', '-C', self.project_dir, 'checkout', version]],
                preview_objects=[version],
                preview_paths=[self.project_dir],
            )
        if action == 'delete' and self.service_units:
            return self._plan_full_delete(action='delete')
        if action == 'delete':
            return ActionPlan(
                commands=[['true']],
                preview_paths=[self.project_dir],
                preview_objects=['project remains on disk; no runtime entity detected'],
            )
        if action == 'full_delete':
            return self._plan_full_delete()
        raise ValueError(f'unsupported project action: {action}')

    def _plan_common_action(self, action: ProjectAction) -> ActionPlan | None:
        if action in {'start', 'stop', 'restart', 'autostart_enable', 'autostart_disable'}:
            units = self.service_units or ([self.service_unit] if self.service_unit else [])
            if not units:
                raise ValueError(f'{action} requires a detected project systemd unit')
            verb = {
                'start': 'start',
                'stop': 'stop',
                'restart': 'restart',
                'autostart_enable': 'enable',
                'autostart_disable': 'disable',
            }[action]
            groups = unit_groups(dict(service_targets=self.service_targets))
            return ActionPlan(
                commands=[[*( ['systemctl', '--user'] if scope == 'user' else ['sudo', 'systemctl']), verb, *names] for scope, names in groups],
                requires_sudo=any(scope == 'system' for scope, _ in groups),
                preview_objects=units, preview_paths=[self.project_dir],
            )
        if action in {'cf_create', 'cf_refresh'}:
            if self.cftunnel_unit:
                return ActionPlan(
                    commands=[['sudo', 'systemctl', 'restart', self.cftunnel_unit]],
                    requires_sudo=True,
                    preview_objects=[self.cftunnel_unit],
                    preview_paths=[self.project_dir],
                )
            if self.cftunnel_script:
                return ActionPlan(
                    commands=[['bash', self.cftunnel_script]],
                    working_dir=self.project_dir,
                    preview_paths=[self.cftunnel_script],
                )
            raise ValueError(f'{action} requires a detected CF tunnel unit or script')
        if action == 'cf_disable':
            if not self.cftunnel_unit:
                raise ValueError('cf_disable requires a detected cftunnel systemd unit')
            return ActionPlan(
                commands=[['sudo', 'systemctl', 'disable', '--now', self.cftunnel_unit]],
                requires_sudo=True,
                preview_objects=[self.cftunnel_unit],
                preview_paths=[self.project_dir],
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

    def _plan_full_delete(self, action: str = 'full_delete') -> ActionPlan:
        path = Path(self.project_dir).absolute()
        stat = path.lstat() if path.exists() or path.is_symlink() else None
        context = dict(project_dir=str(path),
                       directory_identity=[stat.st_dev, stat.st_ino] if stat else None,
                       service_units=self.service_units, service_scope=self.service_scope, service_targets=self.service_targets)
        return ActionPlan(
            commands=[[sys.executable, '-m', 'app.services.project_lifecycle', json.dumps(context), action]],
            working_dir=str(Path(__file__).resolve().parents[2]),
            command_timeout_seconds=180,
            requires_sudo=any(scope == 'system' for scope, _ in unit_groups(context)),
            preview_paths=[self.project_dir], preview_objects=self.service_units,
        )

    def get_version_info(self) -> PackageVersionInfo:
        tags = _read_git_tags(self.project_dir)
        if tags:
            return PackageVersionInfo(
                current_version=self.current_version,
                latest_version=tags[0],
                versions=tags,
                source_status='ok',
                version_source='git_tags',
            )
        if self.git_remote_url:
            remote = self._version_service.get_remote_git_version_info(
                self.git_remote_url,
                current_version=self.current_version,
            )
            if remote.source_status == 'ok' and remote.versions:
                return remote.model_copy(update={'version_source': 'github_tags'})
        return PackageVersionInfo(
            current_version=self.current_version,
            latest_version=None,
            versions=[self.current_version] if self.current_version else [],
            source_status='local',
            error='no local or remote version tags found',
            version_source='git_tags',
        )

    def list_available_versions(self) -> list[str]:
        return self.get_version_info().versions

def _read_git_tags(project_dir: str) -> list[str]:
    git_dir = Path(project_dir) / '.git'
    refs_dir = git_dir / 'refs' / 'tags'
    if not refs_dir.is_dir():
        return []
    tags = sorted((path.name for path in refs_dir.iterdir() if path.is_file()), reverse=True)
    return tags[:50]


def _extract_asset_snapshot_id(asset_snapshot_json: str | None) -> str:
    if not asset_snapshot_json:
        return ''
    marker = '"object_id"'
    start = asset_snapshot_json.find(marker)
    if start == -1:
        return ''
    colon = asset_snapshot_json.find(':', start)
    if colon == -1:
        return ''
    tail = asset_snapshot_json[colon + 1 :].lstrip()
    if not tail.startswith('"'):
        return ''
    tail = tail[1:]
    end = tail.find('"')
    return tail[:end] if end != -1 else ''
