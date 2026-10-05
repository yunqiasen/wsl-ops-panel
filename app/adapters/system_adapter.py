from typing import Literal

from app.adapters.base import ActionPlan
from app.models.assets import PackageVersionInfo

SystemAction = Literal['update_latest', 'deploy_version']


class SystemInfrastructureAdapter:
    def __init__(self, *, name: str, current_version: str | None = None) -> None:
        self.name = name
        self.current_version = current_version

    def plan_action(self, action: SystemAction, version: str | None = None) -> ActionPlan:
        if self.name in {'APT / 系统软件包', 'apt_packages'}:
            if action == 'update_latest':
                return ActionPlan(
                    commands=[['sudo', 'apt', 'update'], ['apt', 'list', '--upgradable']],
                    requires_sudo=True,
                    preview_objects=['APT / 系统软件包', 'check-upgradable'],
                )
            raise ValueError('apt upgrade is intentionally not exposed in deploy_version')
        if self.name == 'cloudflared':
            command = ['/home/div/.cftunnel/bin/cloudflared', 'update']
            if action == 'deploy_version':
                if not version:
                    raise ValueError('deploy_version requires a target version')
                command.extend(['--version', version])
            return ActionPlan(commands=[command], preview_objects=[self.name, version or 'latest'])
        if self.name == 'bun' and action == 'update_latest':
            return ActionPlan(commands=[['bun', 'upgrade']], preview_objects=[self.name, 'latest'])
        if self.name == 'tailscale':
            command = ['tailscale', 'update', '--yes']
            if action == 'deploy_version':
                if not version:
                    raise ValueError('deploy_version requires a target version')
                command.extend(['--version', version])
            return ActionPlan(commands=[command], preview_objects=[self.name, version or 'latest'])
        raise ValueError(f'{self.name} does not support {action}')

    def get_version_info(self) -> PackageVersionInfo:
        versions = [self.current_version] if self.current_version else []
        return PackageVersionInfo(
            current_version=self.current_version,
            versions=versions,
            source_status='manual',
            version_source='system_tool',
        )

    def list_available_versions(self) -> list[str]:
        return [self.current_version] if self.current_version else []
