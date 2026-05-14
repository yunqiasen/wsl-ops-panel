from typing import Literal

from app.adapters.base import ActionPlan

SystemdAction = Literal['start', 'stop', 'restart', 'autostart_enable', 'autostart_disable', 'delete', 'full_delete']


class SystemdUnitAdapter:
    def __init__(self, *, unit_name: str, working_dir: str) -> None:
        self.unit_name = unit_name
        self.working_dir = working_dir

    def plan_action(self, action: SystemdAction, version: str | None = None) -> ActionPlan:
        command_map = {
            'start': ['sudo', 'systemctl', 'start', self.unit_name],
            'stop': ['sudo', 'systemctl', 'stop', self.unit_name],
            'restart': ['sudo', 'systemctl', 'restart', self.unit_name],
            'autostart_enable': ['sudo', 'systemctl', 'enable', self.unit_name],
            'autostart_disable': ['sudo', 'systemctl', 'disable', self.unit_name],
        }
        if action in command_map:
            return ActionPlan(
                commands=[command_map[action]],
                working_dir=self.working_dir,
                requires_sudo=True,
                preview_objects=[self.unit_name],
                preview_paths=[self.working_dir],
            )
        if action == 'delete':
            return ActionPlan(
                commands=[['sudo', 'systemctl', 'disable', '--now', self.unit_name]],
                requires_sudo=True,
                preview_objects=[self.unit_name],
                preview_paths=[self.working_dir],
            )
        if action == 'full_delete':
            raise ValueError('full_delete is not enabled for systemd in phase 1')
        raise ValueError(f'unsupported systemd action: {action}')

    def list_available_versions(self) -> list[str]:
        return []
