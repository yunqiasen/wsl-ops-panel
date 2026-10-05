"""Compose target knowledge shared by discovery, planning and execution.

Container identity checks remain at execution time. Resolving command inputs
never requires the project directory to exist (existing-container actions do
not depend on Compose); deployment validates immediately before mutation.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


def _ordered_paths(root: Path, values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("Compose 文件列表需要数组")
    paths = []
    for value in values:
        path = Path(value)
        paths.append(os.path.abspath(path if path.is_absolute() else root / path))
    return tuple(dict.fromkeys(paths))


@dataclass(frozen=True)
class ComposeTarget:
    project_dir: Path
    project: str | None
    files: tuple[str, ...]
    env_files: tuple[str, ...]

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> "ComposeTarget":
        root = Path(context["project_dir"]).absolute()
        files = context.get("compose_files") or [
            context.get("compose_file") or "docker-compose.yml"
        ]
        return cls(
            root,
            context.get("project"),
            _ordered_paths(root, files),
            _ordered_paths(root, context.get("env_files") or []),
        )

    def command(
        self, *, extra_files: Iterable[str] = (), validate: bool = False
    ) -> list[str]:
        files = _ordered_paths(self.project_dir, [*self.files, *extra_files])
        if validate:
            if not self.project_dir.is_dir():
                raise ValueError(f"Compose 工作目录不存在：{self.project_dir}")
            for file in (*files, *self.env_files):
                if not Path(file).is_file():
                    raise ValueError(f"Compose 文件不存在：{file}")
        command = ["docker", "compose"]
        if self.project:
            command += ["-p", self.project]
        command += ["--project-directory", str(self.project_dir)]
        for file in files:
            command += ["-f", file]
        for file in self.env_files:
            command += ["--env-file", file]
        return command


def capture_compose_context(asset, primary, config) -> dict[str, Any]:
    """Explicit multi-file configuration wins; single-file defaults do not discard observed overrides."""
    labels = primary.labels if primary else {}
    project = config.get("compose_project") or (
        primary.compose_project if primary else None
    )
    files = config.get("compose_files") or [
        v
        for v in labels.get("com.docker.compose.project.config_files", "").split(",")
        if v
    ]
    env_files = config.get("env_files") or [
        v
        for v in labels.get("com.docker.compose.project.environment_file", "").split(
            ","
        )
        if v
    ]
    return {
        "project": project,
        "compose_files": files or [config.get("compose_file") or "docker-compose.yml"],
        "env_files": env_files,
        "containers": [
            {"id": c.id, "name": c.name}
            for c in asset.containers
            if not project or c.compose_project == project
        ],
    }
