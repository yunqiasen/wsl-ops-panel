from typing import Literal

from app.adapters.base import ActionPlan

SystemdAction = Literal['update_latest', 'deploy_version', 'delete', 'full_delete']


class SystemdUnitAdapter:
    def __init__(self, *, unit_name: str, working_dir: str) -> None:
        self.unit_name = unit_name
        self.working_dir = working_dir

    def plan_action(self, action: SystemdAction, version: str | None = None) -> ActionPlan:
        if action == 'delete':
            return ActionPlan(
                commands=[['sudo', 'systemctl', 'disable', '--now', self.unit_name]],
                requires_sudo=True,
                preview_objects=[self.unit_name],
                preview_paths=[self.working_dir],
            )
        if action == 'full_delete':
            raise ValueError('full_delete is not enabled for systemd in phase 1')
        if action in {'update_latest', 'deploy_version'}:
            raise ValueError(f'{action} is not implemented for systemd in phase 1')
        raise ValueError(f'unsupported systemd action: {action}')

    def list_available_versions(self) -> list[str]:
        return []
