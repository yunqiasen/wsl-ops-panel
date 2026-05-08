import json
from pathlib import Path
import subprocess
from typing import Literal

import yaml

from app.adapters.base import ActionPlan
from app.models.assets import PackageVersionInfo
from app.services.docker_versions import DockerVersionService

DockerAction = Literal['update_latest', 'deploy_version', 'delete', 'full_delete']
_OVERRIDE_FILE = '.wsl-ops-panel.override.yml'
_OVERRIDE_WRITER = (
    'from pathlib import Path; import sys; '
    'Path(sys.argv[1]).write_text('
    'f"services:\n  {sys.argv[2]}:\n    image: {sys.argv[3]}\n", encoding="utf-8")'
)
_HEALTHCHECK_COMMAND = (
    'import sys,urllib.request; '
    'r=urllib.request.urlopen(sys.argv[1], timeout=10); '
    'sys.exit(0 if r.status == 200 else 1)'
)


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
        lifecycle_strategy: str = 'compose_pull',
        override_file: str | None = None,
        recipe_repo_dir: str | None = None,
        local_image_repository: str | None = None,
        local_image_tag_template: str | None = None,
        healthcheck_url: str | None = None,
        version_service: DockerVersionService | None = None,
    ) -> None:
        self.project_dir = project_dir
        self.compose_file = compose_file
        self.primary_container = primary_container
        self.compose_service = compose_service
        self.image_repository = image_repository
        self.current_version = current_version
        self.lifecycle_strategy = lifecycle_strategy
        self.override_file = override_file
        self.recipe_repo_dir = recipe_repo_dir
        self.local_image_repository = local_image_repository
        self.local_image_tag_template = local_image_tag_template
        self.healthcheck_url = healthcheck_url
        self._version_service = version_service or DockerVersionService()

    def plan_action(self, action: DockerAction, version: str | None = None) -> ActionPlan:
        if self.lifecycle_strategy == 'compose_pull':
            return self._plan_compose_pull(action, version)
        if self.lifecycle_strategy == 'compose_local_build_git_tag':
            return self._plan_compose_local_build_git_tag(action, version)
        raise ValueError(f'unsupported lifecycle strategy: {self.lifecycle_strategy}')

    def get_version_info(self) -> PackageVersionInfo:
        if self.lifecycle_strategy == 'compose_local_build_git_tag':
            if not self.recipe_repo_dir:
                raise ValueError('compose_local_build_git_tag requires recipe_repo_dir')
            info = self._version_service.get_git_tag_version_info(self.recipe_repo_dir)
        else:
            if not self.image_repository:
                return PackageVersionInfo(
                    current_version=self.current_version,
                    lifecycle_strategy=self.lifecycle_strategy,
                    version_source='registry_tags',
                )
            info = self._version_service.get_registry_tag_version_info(
                self.image_repository,
                current_version=self.current_version,
            )

        return info.model_copy(
            update={
                'lifecycle_strategy': self.lifecycle_strategy,
                'version_source': (
                    'git_tags' if self.lifecycle_strategy == 'compose_local_build_git_tag' else 'registry_tags'
                ),
            }
        )

    def _plan_compose_pull(self, action: DockerAction, version: str | None) -> ActionPlan:
        if action == 'update_latest':
            return ActionPlan(
                commands=[
                    ['docker', 'compose', '-f', self.compose_file, 'pull', self.compose_service],
                    ['docker', 'compose', '-f', self.compose_file, 'up', '-d', '--no-build', self.compose_service],
                ],
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
                    ['python3', '-c', _OVERRIDE_WRITER, _OVERRIDE_FILE, self.compose_service, image_ref],
                    [
                        'docker',
                        'compose',
                        '-f',
                        self.compose_file,
                        '-f',
                        _OVERRIDE_FILE,
                        'up',
                        '-d',
                        '--no-build',
                        self.compose_service,
                    ],
                    ['rm', '-f', _OVERRIDE_FILE],
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
                preview_paths=[self.project_dir, compose_path],
                preview_objects=_unique_preserving_order(preview_objects),
            )

        raise ValueError(f'unsupported docker action: {action}')

    def _plan_compose_local_build_git_tag(self, action: DockerAction, version: str | None) -> ActionPlan:
        if action in {'delete', 'full_delete'}:
            return self._plan_compose_pull(action, version)
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
        commands = [
            ['git', '-C', self.recipe_repo_dir, 'fetch', '--tags', '--force', 'origin'],
            ['git', '-C', self.recipe_repo_dir, 'checkout', version],
            ['docker', 'build', '-t', image_ref, '-f', 'Dockerfile', '.'],
            [
                'env',
                f'WSL_OPS_IMAGE={image_ref}',
                'docker',
                'compose',
                '-f',
                self.compose_file,
                '-f',
                self.override_file,
                'up',
                '-d',
                '--no-build',
                self.compose_service,
            ],
        ]
        if self.healthcheck_url:
            commands.append(['python3', '-c', _HEALTHCHECK_COMMAND, self.healthcheck_url])

        return ActionPlan(
            commands=commands,
            working_dir=self.project_dir,
            preview_objects=[image_ref, version],
        )

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


def _unique_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered
