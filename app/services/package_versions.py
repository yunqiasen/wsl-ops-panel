import json
import subprocess

import httpx

from app.models.assets import PackageVersionInfo


class PackageVersionService:
    def get_node_version_info(self, package_name: str, *, runner=None) -> PackageVersionInfo:
        run = runner or self._run_npm
        try:
            latest = run(['npm', 'view', package_name, 'version']).strip()
            raw_versions = run(['npm', 'view', package_name, 'versions', '--json'])
            payload = json.loads(raw_versions)
            versions = payload if isinstance(payload, list) else [str(payload)]
            return PackageVersionInfo(
                latest_version=latest or None,
                versions=[str(item) for item in versions],
                source_status='ok',
            )
        except Exception as exc:
            if _is_npm_not_found(exc):
                return PackageVersionInfo(source_status='not_found', error='package not found in npm registry')
            return PackageVersionInfo(source_status='error', error=str(exc))

    def get_python_version_info(self, package_name: str, *, fetcher=None) -> PackageVersionInfo:
        get_json = fetcher or self._fetch_pypi_json
        try:
            payload = get_json(package_name)
            releases = payload.get('releases', {})
            versions = sorted(releases.keys())
            latest = payload.get('info', {}).get('version')
            return PackageVersionInfo(latest_version=latest, versions=versions, source_status='ok')
        except Exception as exc:
            if _http_status_code(exc) == 404:
                return PackageVersionInfo(source_status='not_found', error='package not found in PyPI registry')
            return PackageVersionInfo(source_status='error', error=str(exc))

    @staticmethod
    def _run_npm(command: list[str]) -> str:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return completed.stdout

    @staticmethod
    def _fetch_pypi_json(package_name: str) -> dict:
        response = httpx.get(f'https://pypi.org/pypi/{package_name}/json', timeout=10.0)
        response.raise_for_status()
        return response.json()


def _is_npm_not_found(exc: Exception) -> bool:
    if not isinstance(exc, subprocess.CalledProcessError):
        return False
    combined = ((exc.output or '') + '\n' + (exc.stderr or '')).lower()
    return '404' in combined or 'not found' in combined


def _http_status_code(exc: Exception) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    response = getattr(exc, 'response', None)
    status_code = getattr(response, 'status_code', None)
    return status_code if isinstance(status_code, int) else None
