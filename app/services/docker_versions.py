import re
import subprocess

import httpx
from packaging.version import Version

from app.models.assets import PackageVersionInfo, RuntimeVersionInfo


class DockerVersionService:
    def get_git_tag_version_info(self, repo_dir: str, *, runner=None, fetch: bool = True) -> PackageVersionInfo:
        run = runner or self._run
        try:
            if fetch:
                run(['git', '-C', repo_dir, 'fetch', '--tags', '--force', 'origin'])
            current = run(['git', '-C', repo_dir, 'tag', '--points-at', 'HEAD']).strip().splitlines()
            tags = [
                tag
                for tag in run(['git', '-C', repo_dir, 'tag', '--list']).splitlines()
                if re.match(r'^v\d+\.\d+\.\d+$', tag)
            ]
            ordered = sorted(tags, key=lambda item: Version(item[1:]), reverse=True)
            return PackageVersionInfo(
                current_version=current[0] if current else None,
                latest_version=ordered[0] if ordered else None,
                versions=ordered,
                source_status='ok',
            )
        except Exception as exc:
            return PackageVersionInfo(source_status='error', error=str(exc))

    def get_registry_tag_version_info(
        self,
        image_repository: str,
        current_version: str | None = None,
        *,
        fetcher=None,
    ) -> PackageVersionInfo:
        get_json = fetcher or self._fetch_docker_hub_tags
        try:
            payload = get_json(image_repository)
            versions = [row['name'] for row in payload.get('results', []) if row.get('name')]
            latest = versions[0] if versions else None
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=latest,
                versions=versions,
                source_status='ok',
            )
        except Exception as exc:
            return PackageVersionInfo(
                current_version=current_version,
                source_status='error',
                error=str(exc),
            )

    @staticmethod
    def build_runtime_version_info(container) -> RuntimeVersionInfo:
        if container is None:
            return RuntimeVersionInfo()
        return RuntimeVersionInfo(
            image=container.image,
            image_tag=container.image_tag,
            oci_version=container.labels.get('org.opencontainers.image.version'),
            oci_revision=container.labels.get('org.opencontainers.image.revision'),
            ports=container.ports,
        )

    @staticmethod
    def _run(command: list[str]) -> str:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return completed.stdout

    @staticmethod
    def _fetch_docker_hub_tags(image_repository: str) -> dict:
        response = httpx.get(
            f'https://hub.docker.com/v2/repositories/{image_repository}/tags?page_size=25',
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()
