from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse


def build_source_links(
    *,
    git_remote_url: str | None = None,
    image_repository: str | None = None,
    package_name: str | None = None,
    package_manager: str | None = None,
    labels: dict[str, str] | None = None,
    homepage: str | None = None,
    repository_url: str | None = None,
) -> dict[str, str]:
    links: dict[str, str] = {}
    labels = labels or {}

    source_url = labels.get('org.opencontainers.image.source') or repository_url or git_remote_url
    github = normalize_git_remote_url(source_url)
    if github and _is_github_url(github):
        links['github'] = github

    docker = docker_repository_url(image_repository)
    if docker:
        links['docker'] = docker

    if package_name and package_manager == 'npm':
        links['npm'] = npm_package_url(package_name)
    if package_name and package_manager == 'pip':
        links['pypi'] = pypi_package_url(package_name)

    homepage_url = labels.get('org.opencontainers.image.url') or homepage
    if homepage_url and _looks_like_url(homepage_url):
        links['homepage'] = homepage_url.strip()

    return links


def source_url_from_links(links: dict[str, str] | None) -> str | None:
    if not links:
        return None
    for key in ('github', 'docker', 'npm', 'pypi', 'homepage'):
        value = links.get(key)
        if value:
            return value
    return None


def normalize_git_remote_url(value: str | None) -> str | None:
    if not value:
        return None
    url = value.strip()
    if not url:
        return None
    if url.startswith('git+'):
        url = url[4:]
    if url.startswith('git@github.com:'):
        path = url.split(':', 1)[1]
        return _strip_git_suffix(f'https://github.com/{path}')
    if url.startswith('ssh://git@github.com/'):
        path = url.removeprefix('ssh://git@github.com/')
        return _strip_git_suffix(f'https://github.com/{path}')
    if url.startswith('https://github.com/') or url.startswith('http://github.com/'):
        parsed = urlparse(url)
        return _strip_git_suffix(f'https://github.com{parsed.path}')
    return url


def docker_repository_url(repository: str | None) -> str | None:
    if not repository:
        return None
    repo = repository.split('@', 1)[0].strip()
    if not repo:
        return None
    last_slash = repo.rfind('/')
    last_colon = repo.rfind(':')
    if last_colon > last_slash:
        repo = repo[:last_colon]

    if repo.startswith('ghcr.io/'):
        parts = repo.split('/')
        if len(parts) >= 3:
            owner = parts[1]
            package = parts[-1]
            return f'https://github.com/{owner}/{package}/pkgs/container/{package}'
        return f'https://{repo}'

    if repo.startswith('docker.io/'):
        repo = repo.removeprefix('docker.io/')
    if '/' not in repo:
        repo = f'library/{repo}'
    if _is_known_registry_repo(repo):
        return f'https://{repo}'
    return f'https://hub.docker.com/r/{repo}'


def npm_package_url(package_name: str) -> str:
    return f'https://www.npmjs.com/package/{quote(package_name, safe="")}'


def pypi_package_url(package_name: str) -> str:
    return f'https://pypi.org/project/{quote(package_name)}/'


def source_links_from_package_json(payload: dict[str, Any], *, git_remote_url: str | None = None) -> dict[str, str]:
    repository = payload.get('repository')
    repository_url = None
    if isinstance(repository, str):
        repository_url = repository
    elif isinstance(repository, dict) and isinstance(repository.get('url'), str):
        repository_url = repository['url']

    homepage = payload.get('homepage') if isinstance(payload.get('homepage'), str) else None
    return build_source_links(
        git_remote_url=git_remote_url,
        repository_url=repository_url,
        homepage=homepage,
    )


def _strip_git_suffix(url: str) -> str:
    cleaned = url.strip().removesuffix('.git')
    parsed = urlparse(cleaned)
    if parsed.netloc != 'github.com':
        return cleaned
    path = PurePosixPath(parsed.path)
    parts = [part for part in path.parts if part != '/']
    if len(parts) >= 2:
        return f'https://github.com/{parts[0]}/{parts[1]}'
    return cleaned.rstrip('/')


def _is_github_url(url: str) -> bool:
    return urlparse(url).netloc.lower() == 'github.com'


def _looks_like_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {'http', 'https'} and bool(parsed.netloc)


def _is_known_registry_repo(repo: str) -> bool:
    first = repo.split('/', 1)[0]
    return '.' in first or ':' in first or first == 'localhost'
