from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Mapping, Any

DESCRIPTION_LABELS = (
    'org.opencontainers.image.description',
    'description',
)
README_NAMES = ('README.md', 'README.MD', 'README', 'readme.md')
MAX_DESCRIPTION_CHARS = 220


def resolve_asset_description(
    project_path: str | Path | None,
    *,
    configured: str | None = None,
    labels: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve a short human project description without trusting one source blindly."""
    for value in (
        configured,
        _description_from_labels(labels or {}),
        _description_from_package_json(project_path),
        _description_from_pyproject(project_path),
        _description_from_readme(project_path),
    ):
        cleaned = _clean_description(value)
        if cleaned:
            return cleaned
    return None


def _description_from_labels(labels: Mapping[str, str]) -> str | None:
    for key in DESCRIPTION_LABELS:
        value = labels.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _description_from_package_json(project_path: str | Path | None) -> str | None:
    path = _project_path(project_path)
    if path is None:
        return None
    package_json = path / 'package.json'
    if not package_json.exists():
        return None
    try:
        payload = json.loads(package_json.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get('description')
    return value if isinstance(value, str) else None


def _description_from_pyproject(project_path: str | Path | None) -> str | None:
    path = _project_path(project_path)
    if path is None:
        return None
    pyproject = path / 'pyproject.toml'
    if not pyproject.exists():
        return None
    try:
        payload = tomllib.loads(pyproject.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    project = payload.get('project') if isinstance(payload, dict) else None
    if not isinstance(project, dict):
        return None
    value = project.get('description')
    return value if isinstance(value, str) else None


def _description_from_readme(project_path: str | Path | None) -> str | None:
    path = _project_path(project_path)
    if path is None:
        return None
    for name in README_NAMES:
        readme = path / name
        if not readme.exists():
            continue
        try:
            text = readme.read_text(encoding='utf-8', errors='replace')[:4096]
        except OSError:
            continue
        lines = [_strip_markdown_line(line) for line in text.splitlines()]
        lines = [line for line in lines if line]
        if lines:
            return ' '.join(lines[:2])
    return None


def _project_path(project_path: str | Path | None) -> Path | None:
    if project_path is None:
        return None
    path = Path(project_path)
    return path if path.is_dir() else None


def _strip_markdown_line(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return ''
    stripped = stripped.lstrip('#').strip()
    if stripped in {'---', '***'}:
        return ''
    return stripped


def _clean_description(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = ' '.join(value.strip().split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_DESCRIPTION_CHARS:
        return cleaned[: MAX_DESCRIPTION_CHARS - 1].rstrip() + '…'
    return cleaned
