from typing import Literal

from app.adapters.base import ActionPlan
from app.models.assets import PackageVersionInfo
from app.services.package_versions import PackageVersionService

PythonAction = Literal['update_latest', 'deploy_version', 'delete', 'full_delete']


class PythonPackageAdapter:
    def __init__(
        self,
        *,
        package_name: str,
        current_version: str | None,
        full_delete_paths: list[str],
        version_service: PackageVersionService | None = None,
    ) -> None:
        self.package_name = package_name
        self.current_version = current_version
        self.full_delete_paths = full_delete_paths
        self._version_service = version_service or PackageVersionService()

    def plan_action(self, action: PythonAction, version: str | None = None) -> ActionPlan:
        if action == 'update_latest':
            return ActionPlan(commands=[['python3', '-m', 'pip', 'install', '-U', self.package_name]])
        if action == 'deploy_version':
            if not version:
                raise ValueError('deploy_version requires a target version')
            return ActionPlan(
                commands=[['python3', '-m', 'pip', 'install', f'{self.package_name}=={version}']],
                preview_objects=[f'{self.package_name}=={version}'],
            )
        if action == 'delete':
            return ActionPlan(commands=[['python3', '-m', 'pip', 'uninstall', '-y', self.package_name]])
        if action == 'full_delete':
            commands = [['python3', '-m', 'pip', 'uninstall', '-y', self.package_name]]
            for path in self.full_delete_paths:
                commands.append(['rm', '-rf', path])
            return ActionPlan(
                commands=commands,
                preview_objects=[self.package_name],
                preview_paths=self.full_delete_paths,
            )
        raise ValueError(f'unsupported python action: {action}')

    def get_version_info(self) -> PackageVersionInfo:
        info = self._version_service.get_python_version_info(self.package_name)
        return info.model_copy(update={'current_version': self.current_version})

    def list_available_versions(self) -> list[str]:
        return self.get_version_info().versions
