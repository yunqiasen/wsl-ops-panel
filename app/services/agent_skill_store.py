from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from app.services.agent_paths import resolve_agent_paths
from app.services.agent_skills import (
    SKILL_METADATA_FILENAME,
    _materialize_skill_source,
    _validate_skill_root,
    install_skill_to_home,
    safe_skill_name,
    uninstall_skill_from_home,
)
from app.services.state_store import PanelStateStore


class SkillAssignedError(RuntimeError):
    """Raised when a managed Skill still has enabled client assignments."""


class AgentSkillStore:
    """Database metadata + filesystem SSOT for managed Agent Skills."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.data_root.chmod(0o700)
        self.ssot_root = self.data_root / "skills"
        self.ssot_root.mkdir(parents=True, exist_ok=True)
        self.ssot_root.chmod(0o700)
        self.backup_root = self.data_root / "skill-backups"
        self.state = PanelStateStore(self.data_root)

    def list(self) -> dict[str, dict[str, Any]]:
        return self.state.list_skills()

    def get(self, skill_id: str) -> dict[str, Any] | None:
        return self.list().get(skill_id)

    def import_source(
        self,
        skill_id: str,
        name: str,
        source: str,
        *,
        description: str | None = None,
        version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_id = safe_skill_name(skill_id)
        if not clean_id:
            raise ValueError("skill_id is invalid")
        source_value = source.strip()
        if not source_value:
            raise ValueError("source is required")

        workspace = Path(
            tempfile.mkdtemp(prefix=f".{clean_id}.import-", dir=self.data_root)
        )
        staged = Path(
            tempfile.mkdtemp(prefix=f".{clean_id}.stage-", dir=self.ssot_root)
        )
        # tempfile created a directory, while copytree requires a missing target.
        staged.rmdir()
        target = self.ssot_root / clean_id
        backup: Path | None = None
        try:
            source_root, source_kind, _cleanup = _materialize_skill_source(
                source_value, mode="copy", workspace=workspace
            )
            _validate_skill_root(source_root)
            shutil.copytree(
                source_root,
                staged,
                symlinks=False,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            _validate_skill_root(staged)
            content_hash = skill_content_hash(staged)
            if target.exists() or target.is_symlink():
                self.backup_root.mkdir(parents=True, exist_ok=True)
                self.backup_root.chmod(0o700)
                backup = self.backup_root / f"{clean_id}.{_safe_stamp()}"
                os.replace(target, backup)
            os.replace(staged, target)
            try:
                saved = self.state.upsert_skill(
                    skill_id=clean_id,
                    name=name.strip() or clean_id,
                    description=description,
                    source=source_value,
                    source_kind=source_kind,
                    ssot_path=str(target),
                    version=version,
                    content_hash=content_hash,
                    metadata=metadata or {},
                )
            except Exception:
                shutil.rmtree(target, ignore_errors=True)
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                raise
            return saved
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)

    def import_from_client(
        self,
        home: Path | str,
        client_id: str,
        skill_name: str,
        *,
        skill_id: str | None = None,
        name: str | None = None,
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        clean_name = safe_skill_name(skill_name)
        clean_id = safe_skill_name(skill_id or skill_name)
        if not clean_name or not clean_id:
            raise ValueError("skill name is invalid")
        paths = resolve_agent_paths(
            client_id,
            Path(home).expanduser(),
            environ=environ,
            overrides=overrides,
        )
        if paths.skills is None:
            raise ValueError(f"{client_id} 暂不支持 Skill 写入")
        source = paths.skills / clean_name
        if not source.is_dir():
            raise FileNotFoundError(f"Skill 不存在: {source}")
        saved = self.import_source(clean_id, name or clean_name, str(source))
        self.state.upsert_skill_variant(
            clean_id,
            client_id,
            platform,
            {"directory": clean_name, "mode": "copy"},
        )
        self.state.set_skill_assignment(
            "__local__",
            client_id,
            clean_id,
            variant_client_id=client_id,
            variant_platform=platform,
        )
        observed_hash = skill_content_hash(source)
        self.state.upsert_skill_observation(
            "__local__",
            client_id,
            clean_id,
            present=True,
            content_hash=observed_hash,
            status=(
                "installed"
                if observed_hash == str(saved["content_hash"])
                else "drifted"
            ),
        )
        return saved

    def refresh_client_observations(
        self,
        home: Path | str,
        client_id: str,
        *,
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> list[dict[str, Any]]:
        paths = resolve_agent_paths(
            client_id,
            Path(home).expanduser(),
            environ=environ,
            overrides=overrides,
        )
        if paths.skills is None:
            return []
        assigned = {
            str(item["skill_id"])
            for item in self.state.list_skill_assignments("__local__", client_id)
            if item.get("desired_enabled", True)
        }
        observations: list[dict[str, Any]] = []
        for skill_id, skill in self.list().items():
            variant = self._variant(skill_id, client_id, platform)
            directory = safe_skill_name(str(variant.get("directory") or skill_id))
            target = paths.skills / directory
            try:
                present = target.is_dir()
                if present:
                    content_hash = skill_content_hash(target)
                    status = (
                        "installed"
                        if content_hash == str(skill.get("content_hash") or "")
                        else "drifted"
                    )
                    observations.append(
                        {
                            "skill_id": skill_id,
                            "present": True,
                            "content_hash": content_hash,
                            "status": status,
                        }
                    )
                elif skill_id in assigned:
                    observations.append(
                        {
                            "skill_id": skill_id,
                            "present": False,
                            "content_hash": None,
                            "status": "missing",
                        }
                    )
            except OSError as exc:
                observations.append(
                    {
                        "skill_id": skill_id,
                        "present": False,
                        "content_hash": None,
                        "status": "error",
                        "error": str(exc),
                    }
                )
        self.state.replace_skill_observations(
            "__local__", client_id, observations
        )
        return observations

    def install_to_client(
        self,
        home: Path | str,
        skill_id: str,
        client_id: str,
        *,
        mode: str = "copy",
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        skill = self.get(skill_id)
        if skill is None:
            raise FileNotFoundError(f"Skill 资源不存在: {skill_id}")
        source = Path(str(skill["ssot_path"]))
        if not source.is_dir():
            raise FileNotFoundError(f"Skill SSOT 不存在: {source}")
        variant = self._variant(skill_id, client_id, platform)
        directory = safe_skill_name(str(variant.get("directory") or skill_id))
        selected_mode = str(variant.get("mode") or mode).strip().lower()
        result = install_skill_to_home(
            home,
            client_id,
            directory,
            str(source),
            mode=selected_mode,
            environ=environ,
            overrides=overrides,
        )
        self.state.upsert_skill_variant(
            skill_id,
            client_id,
            platform,
            {"directory": directory, "mode": selected_mode},
        )
        self.state.set_skill_assignment(
            "__local__",
            client_id,
            skill_id,
            variant_client_id=client_id,
            variant_platform=platform,
        )
        observed_hash = skill_content_hash(Path(str(result["path"])))
        self.state.upsert_skill_observation(
            "__local__",
            client_id,
            skill_id,
            present=True,
            content_hash=observed_hash,
            status=(
                "installed"
                if observed_hash == str(skill["content_hash"])
                else "drifted"
            ),
        )
        result["skill_id"] = skill_id
        result["content_hash"] = observed_hash
        return result

    def uninstall_from_client(
        self,
        home: Path | str,
        skill_id: str,
        client_id: str,
        *,
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        skill = self.get(skill_id)
        if skill is None:
            raise FileNotFoundError(f"Skill 资源不存在: {skill_id}")
        variant = self._variant(skill_id, client_id, platform)
        directory = safe_skill_name(str(variant.get("directory") or skill_id))
        result = uninstall_skill_from_home(
            home,
            client_id,
            directory,
            environ=environ,
            overrides=overrides,
        )
        self.state.remove_skill_assignments("__local__", client_id, {skill_id})
        self.state.remove_skill_observations("__local__", client_id, {skill_id})
        result["skill_id"] = skill_id
        return result

    def delete(self, skill_id: str, *, force: bool = False) -> bool:
        skill = self.get(skill_id)
        if skill is None:
            return False
        assignments = [
            row
            for row in self.state.list_skill_assignments()
            if row["skill_id"] == skill_id and row["desired_enabled"]
        ]
        if assignments and not force:
            raise SkillAssignedError(f"Skill 仍分配给 {len(assignments)} 个客户端")
        deleted = self.state.delete_skill(skill_id)
        if deleted:
            shutil.rmtree(Path(str(skill["ssot_path"])), ignore_errors=True)
        return deleted

    def _variant(self, skill_id: str, client_id: str, platform: str) -> dict[str, Any]:
        variants = self.state.list_skill_variants(skill_id, client_id, platform)
        if variants:
            install = variants[0].get("install")
            return dict(install) if isinstance(install, dict) else {}
        return {}


def skill_content_hash(root: Path) -> str:
    """Return a stable content hash that excludes manager metadata and VCS data."""
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        if ".git" in relative.parts or "__pycache__" in relative.parts:
            continue
        if path.name == SKILL_METADATA_FILENAME or path.suffix == ".pyc":
            continue
        if not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
