from typing import Literal

from app.adapters.base import ActionPlan

HostPortAction = Literal['start', 'stop']


class HostPortAdapter:
    def __init__(
        self,
        *,
        port: str,
        owner_type: str | None = None,
        target_asset_id: str | None = None,
        target_unit_name: str | None = None,
        target_container_name: str | None = None,
        pid: int | None = None,
        process_name: str | None = None,
    ) -> None:
        self.port = port
        self.owner_type = owner_type or 'unknown'
        self.target_asset_id = target_asset_id
        self.target_unit_name = target_unit_name
        self.target_container_name = target_container_name
        self.pid = pid
        self.process_name = process_name

    def plan_action(self, action: HostPortAction, version: str | None = None) -> ActionPlan:
        if action == 'stop':
            return self._plan_stop()
        if action == 'start':
            return self._plan_start()
        raise ValueError(f'unsupported host port action: {action}')

    def list_available_versions(self) -> list[str]:
        return []

    def _plan_stop(self) -> ActionPlan:
        if self.owner_type == 'docker' and self.target_container_name:
            return ActionPlan(
                commands=[['docker', 'stop', self.target_container_name]],
                preview_objects=[f'port:{self.port}', self.target_container_name],
            )
        if self.owner_type == 'docker' and self.target_asset_id:
            return ActionPlan(
                commands=[['python3', '-m', 'app.tools.host_port_control', 'stop', '--asset-id', self.target_asset_id]],
                working_dir='.',
                preview_objects=[f'port:{self.port}', self.target_asset_id],
            )
        if self.owner_type == 'systemd' and self.target_unit_name:
            return ActionPlan(
                commands=[['sudo', 'systemctl', 'stop', self.target_unit_name]],
                requires_sudo=True,
                preview_objects=[f'port:{self.port}', self.target_unit_name],
            )
        if self.pid:
            return ActionPlan(
                commands=[['kill', str(self.pid)]],
                preview_objects=[f'port:{self.port}', f'pid:{self.pid}', self.process_name or 'process'],
            )
        raise ValueError(f'port {self.port} does not have a stoppable owner')

    def _plan_start(self) -> ActionPlan:
        if self.owner_type == 'docker' and self.target_container_name:
            return ActionPlan(
                commands=[['docker', 'start', self.target_container_name]],
                preview_objects=[f'port:{self.port}', self.target_container_name],
            )
        if self.owner_type == 'docker' and self.target_asset_id:
            return ActionPlan(
                commands=[['python3', '-m', 'app.tools.host_port_control', 'start', '--asset-id', self.target_asset_id]],
                working_dir='.',
                preview_objects=[f'port:{self.port}', self.target_asset_id],
            )
        if self.owner_type == 'systemd' and self.target_unit_name:
            return ActionPlan(
                commands=[['sudo', 'systemctl', 'start', self.target_unit_name]],
                requires_sudo=True,
                preview_objects=[f'port:{self.port}', self.target_unit_name],
            )
        raise ValueError(f'port {self.port} does not have a startable owner')
