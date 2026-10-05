import re
import subprocess
import time
from collections.abc import Callable
from urllib.parse import quote

import httpx
from packaging.version import InvalidVersion, Version

from app.models.assets import PackageVersionInfo, RuntimeVersionInfo


REMOTE_TAG_REF_NAMESPACE = 'wsl-ops-panel/remote-tags'


class DockerVersionService:
    def get_git_tag_version_info(self, repo_dir: str, *, runner=None, fetch: bool = False, remote: bool = False) -> PackageVersionInfo:
        run = runner or self._run
        try:
            if fetch:
                try:
                    run(['git', '-C', repo_dir, 'fetch', '--tags', '--force', 'origin'])
                except Exception:
                    pass
            current = run(['git', '-C', repo_dir, 'tag', '--points-at', 'HEAD']).strip().splitlines()
            tags = self._list_git_tags(repo_dir, run=run, remote=remote)
            ordered = sorted(tags, key=lambda item: Version(item[1:]), reverse=True)
            return PackageVersionInfo(
                current_version=current[0] if current else None,
                latest_version=ordered[0] if ordered else None,
                versions=ordered,
                source_status='ok',
                version_source='git_tags',
            )
        except Exception as exc:
            return PackageVersionInfo(source_status='error', error=str(exc), version_source='git_tags')

    @staticmethod
    def _list_git_tags(repo_dir: str, *, run: Callable[[list[str]], str], remote: bool) -> list[str]:
        if remote:
            try:
                remote_rows = run(
                    with_timeout([
                        'git',
                        '-C',
                        repo_dir,
                        '-c',
                        'http.version=HTTP/1.1',
                        'ls-remote',
                        '--tags',
                        '--refs',
                        'origin',
                        'v*',
                    ])
                ).splitlines()
                remote_tags = [_parse_ls_remote_tag(row) for row in remote_rows if row.strip() and 'refs/tags/' in row]
                tags = [tag for tag in remote_tags if re.match(r'^v\d+\.\d+\.\d+$', tag)]
                if tags:
                    return tags
            except Exception:
                pass
        return [
            tag
            for tag in run(['git', '-C', repo_dir, 'tag', '--list']).splitlines()
            if re.match(r'^v\d+\.\d+\.\d+$', tag)
        ]

    def get_registry_tag_version_info(
        self,
        image_repository: str,
        current_version: str | None = None,
        *,
        source_url: str | None = None,
        fetcher=None,
        runner=None,
    ) -> PackageVersionInfo:
        registry = self._classify_image_repository(image_repository)
        if registry['kind'] == 'local':
            local_version = current_version or 'local'
            return PackageVersionInfo(
                current_version=local_version,
                latest_version=local_version,
                versions=[local_version],
                source_status='local',
                version_source='local_image',
            )
        if registry['kind'] == 'unsupported':
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=current_version,
                versions=[current_version] if current_version else [],
                source_status='unsupported',
                error=f"unsupported registry: {image_repository}",
                version_source='registry_tags',
            )

        get_json = fetcher or (self._fetch_ghcr_tags if registry['kind'] == 'ghcr' else self._fetch_docker_hub_tags)
        version_source = 'ghcr_tags' if registry['kind'] == 'ghcr' else 'registry_tags'
        try:
            payload = self._fetch_with_retry(get_json, registry['lookup'])
            versions = self._extract_registry_versions(payload)
            latest = self._select_latest_tag(versions)
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=latest,
                versions=versions,
                source_status='ok',
                version_source=version_source,
            )
        except Exception as exc:
            fallback = self._registry_error_fallback(
                exc,
                current_version=current_version,
                version_source=version_source,
                source_url=source_url,
                runner=runner,
            )
            if fallback is not None:
                return fallback
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=current_version,
                versions=[current_version] if current_version else [],
                source_status='error',
                error=str(exc),
                version_source=version_source,
            )


    def get_remote_git_version_info(
        self,
        git_url: str,
        *,
        current_version: str | None = None,
        runner=None,
    ) -> PackageVersionInfo:
        run = runner or self._run
        normalized_url = _normalize_github_url(git_url)
        if not normalized_url or not _is_http_git_url(normalized_url):
            return PackageVersionInfo(
                current_version=current_version,
                versions=[current_version] if current_version else [],
                source_status='unsupported',
                error='unsupported git source url',
                version_source='github_tags',
            )
        try:
            rows = run(
                with_timeout([
                    'git',
                    '-c',
                    'http.version=HTTP/1.1',
                    'ls-remote',
                    '--tags',
                    '--refs',
                    normalized_url,
                ])
            ).splitlines()
            tags = [_parse_ls_remote_tag(row) for row in rows if row.strip() and 'refs/tags/' in row]
            versions = _sort_version_tags([tag for tag in tags if _looks_like_version_tag(tag)])[:50]
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=versions[0] if versions else None,
                versions=versions,
                source_status='ok' if versions else 'local',
                error=None if versions else 'no version tags found from git source',
                version_source='github_tags',
            )
        except Exception as exc:
            return PackageVersionInfo(
                current_version=current_version,
                versions=[current_version] if current_version else [],
                source_status='error',
                error=f'git source lookup failed: {exc}',
                version_source='github_tags',
            )

    def _registry_error_fallback(
        self,
        exc: Exception,
        *,
        current_version: str | None,
        version_source: str,
        source_url: str | None,
        runner=None,
    ) -> PackageVersionInfo | None:
        status = _http_status_code(exc)
        if source_url:
            git_info = self.get_remote_git_version_info(source_url, current_version=current_version, runner=runner)
            if git_info.source_status == 'ok' and git_info.versions:
                return git_info
        if status == 404:
            local_version = current_version or 'local'
            return PackageVersionInfo(
                current_version=local_version,
                latest_version=None,
                versions=[local_version],
                source_status='local',
                error='image repository not published; treated as local build image',
                version_source='local_image',
            )
        if status in {401, 403}:
            return PackageVersionInfo(
                current_version=current_version,
                latest_version=None,
                versions=[current_version] if current_version else [],
                source_status='auth_required',
                error='registry requires authentication or tags are not public',
                version_source=version_source,
            )
        return None

    @staticmethod
    def build_runtime_version_info(container) -> RuntimeVersionInfo:
        if container is None:
            return RuntimeVersionInfo()
        return RuntimeVersionInfo(
            image=container.image,
            image_tag=container.image_tag,
            oci_version=container.labels.get('org.opencontainers.image.version') or container.labels.get('version'),
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

    @staticmethod
    def _fetch_ghcr_tags(package_path: str) -> dict:
        response = httpx.get(
            f'https://ghcr.io/v2/{package_path}/tags/list',
            timeout=10.0,
            headers={'Accept': 'application/json'},
        )
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _fetch_with_retry(fetcher: Callable[[str], dict], image_repository: str) -> dict:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return fetcher(image_repository)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError('registry fetch failed')

    @staticmethod
    def _extract_registry_versions(payload: dict) -> list[str]:
        results = payload.get('results')
        if isinstance(results, list):
            return [str(row['name']) for row in results if isinstance(row, dict) and row.get('name')]
        tags = payload.get('tags')
        if isinstance(tags, list):
            return [str(tag) for tag in tags if str(tag)]
        return []

    def _select_latest_tag(self, versions: list[str]) -> str | None:
        semver_versions = [tag for tag in versions if self._is_semver(tag)]
        return max(semver_versions, key=Version) if semver_versions else (versions[0] if versions else None)

    @staticmethod
    def _classify_image_repository(image_repository: str) -> dict[str, str]:
        repo = image_repository.split('@', 1)[0].strip()
        if not repo:
            return {'kind': 'unsupported', 'lookup': image_repository}
        last_slash = repo.rfind('/')
        last_colon = repo.rfind(':')
        if last_colon > last_slash:
            repo = repo[:last_colon]

        if repo.startswith(('local/', 'localhost/', '127.0.0.1:', '0.0.0.0:')):
            return {'kind': 'local', 'lookup': repo}
        if repo.startswith('ghcr.io/'):
            return {'kind': 'ghcr', 'lookup': quote(repo.removeprefix('ghcr.io/'), safe='/')}
        if repo.startswith('docker.io/'):
            repo = repo.removeprefix('docker.io/')
        if '/' not in repo:
            return {'kind': 'docker_hub', 'lookup': f'library/{repo}'}
        registry_host = repo.split('/', 1)[0]
        if '.' in registry_host or ':' in registry_host:
            return {'kind': 'unsupported', 'lookup': repo}
        return {'kind': 'docker_hub', 'lookup': repo}

    @staticmethod
    def _is_semver(tag: str) -> bool:
        try:
            Version(tag)
        except InvalidVersion:
            return False
        return True



def _http_status_code(exc: Exception) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    response = getattr(exc, 'response', None)
    status_code = getattr(response, 'status_code', None)
    return status_code if isinstance(status_code, int) else None


def _normalize_github_url(value: str | None) -> str | None:
    if not value:
        return None
    url = value.strip().removeprefix('git+')
    if url.startswith('git@github.com:'):
        url = 'https://github.com/' + url.removeprefix('git@github.com:')
    if url.startswith('ssh://git@github.com/'):
        url = 'https://github.com/' + url.removeprefix('ssh://git@github.com/')
    return url.removesuffix('.git')


def _is_http_git_url(value: str) -> bool:
    return value.startswith(('https://', 'http://'))


def _looks_like_version_tag(tag: str) -> bool:
    return re.match(r'^v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$', tag) is not None


def _sort_version_tags(tags: list[str]) -> list[str]:
    unique = list(dict.fromkeys(tags))

    def key(tag: str):
        try:
            return Version(tag.removeprefix('v'))
        except InvalidVersion:
            return Version('0')

    return sorted(unique, key=key, reverse=True)


def _parse_ls_remote_tag(row: str) -> str:
    ref = row.rsplit('refs/tags/', 1)[-1]
    return ref.rsplit('/', 1)[-1] if ref.startswith(f'{REMOTE_TAG_REF_NAMESPACE}/') else ref


def with_timeout(command: list[str], seconds: int = 20) -> list[str]:
    return ['timeout', str(seconds), *command]
