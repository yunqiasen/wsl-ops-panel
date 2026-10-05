from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx


@dataclass(frozen=True)
class PackageMetadata:
    status: str
    description: str | None = None
    homepage: str | None = None
    repository: str | None = None
    package_url: str | None = None
    license: str | None = None
    keywords: list[str] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'description': self.description,
            'homepage': self.homepage,
            'repository': self.repository,
            'package_url': self.package_url,
            'license': self.license,
            'keywords': self.keywords or [],
            'error': self.error,
        }


class PackageMetadataService:
    def __init__(self, *, ttl_seconds: int = 1800) -> None:
        self._ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str], tuple[float, PackageMetadata]] = {}

    def get_node_metadata(self, package_name: str, *, fetcher: Callable[[str], dict[str, Any]] | None = None) -> PackageMetadata:
        return self._get_cached('node', package_name, lambda: self._fetch_node(package_name, fetcher=fetcher))

    def get_python_metadata(self, package_name: str, *, fetcher: Callable[[str], dict[str, Any]] | None = None) -> PackageMetadata:
        return self._get_cached('python', package_name, lambda: self._fetch_python(package_name, fetcher=fetcher))

    def _get_cached(self, kind: str, package_name: str, loader: Callable[[], PackageMetadata]) -> PackageMetadata:
        key = (kind, package_name.lower())
        now = time.time()
        cached = self._cache.get(key)
        if cached is not None and now - cached[0] < self._ttl_seconds:
            return cached[1]
        value = loader()
        self._cache[key] = (now, value)
        return value

    def _fetch_node(self, package_name: str, *, fetcher: Callable[[str], dict[str, Any]] | None = None) -> PackageMetadata:
        package_url_name = package_name.replace('/', '%2F')
        try:
            payload = (fetcher or self._fetch_npm_registry)(package_name)
            repository = _normalize_repository(payload.get('repository'))
            keywords = payload.get('keywords') if isinstance(payload.get('keywords'), list) else []
            return PackageMetadata(
                status='ok',
                description=_string_or_none(payload.get('description')),
                homepage=_string_or_none(payload.get('homepage')),
                repository=repository,
                package_url=f'https://www.npmjs.com/package/{package_url_name}',
                license=_string_or_none(payload.get('license')),
                keywords=[str(item) for item in keywords[:12]],
            )
        except Exception as exc:
            return PackageMetadata(status='error', package_url=f'https://www.npmjs.com/package/{package_url_name}', error=str(exc))

    def _fetch_python(self, package_name: str, *, fetcher: Callable[[str], dict[str, Any]] | None = None) -> PackageMetadata:
        try:
            payload = (fetcher or self._fetch_pypi_json)(package_name)
            info = payload.get('info') if isinstance(payload.get('info'), dict) else {}
            project_urls = info.get('project_urls') if isinstance(info.get('project_urls'), dict) else {}
            repository = _first_url(project_urls, ('Source', 'Source Code', 'Repository', 'Homepage'))
            homepage = _string_or_none(info.get('home_page')) or _first_url(project_urls, ('Homepage', 'Home'))
            keywords = str(info.get('keywords') or '').replace(',', ' ').split()
            return PackageMetadata(
                status='ok',
                description=_string_or_none(info.get('summary')) or _string_or_none(info.get('description')),
                homepage=homepage,
                repository=repository,
                package_url=f'https://pypi.org/project/{package_name}/',
                license=_string_or_none(info.get('license')),
                keywords=keywords[:12],
            )
        except Exception as exc:
            return PackageMetadata(status='error', package_url=f'https://pypi.org/project/{package_name}/', error=str(exc))

    @staticmethod
    def _fetch_npm_registry(package_name: str) -> dict[str, Any]:
        response = httpx.get(f'https://registry.npmjs.org/{package_name}', timeout=6.0)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _fetch_pypi_json(package_name: str) -> dict[str, Any]:
        response = httpx.get(f'https://pypi.org/pypi/{package_name}/json', timeout=6.0)
        response.raise_for_status()
        return response.json()


def _string_or_none(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _normalize_repository(value: Any) -> str | None:
    if isinstance(value, str):
        return _clean_git_url(value)
    if isinstance(value, dict):
        return _clean_git_url(_string_or_none(value.get('url')))
    return None


def _clean_git_url(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.removeprefix('git+').removesuffix('.git')
    if cleaned.startswith('git://github.com/'):
        cleaned = 'https://github.com/' + cleaned.removeprefix('git://github.com/')
    if cleaned.startswith('git@github.com:'):
        cleaned = 'https://github.com/' + cleaned.removeprefix('git@github.com:')
    return cleaned


def _first_url(project_urls: dict[Any, Any], labels: tuple[str, ...]) -> str | None:
    normalized = {str(key).lower(): value for key, value in project_urls.items()}
    for label in labels:
        value = normalized.get(label.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in project_urls.values():
        if isinstance(value, str) and value.strip().startswith(('http://', 'https://')):
            return value.strip()
    return None
