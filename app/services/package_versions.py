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
