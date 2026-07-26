from __future__ import annotations

from pathlib import Path, PurePosixPath
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.services.agent_clients import AGENT_CLIENTS, get_agent_client


def scan_agent_skills(home: Path | None = None) -> dict[str, dict[str, Any]]:
    home = home or Path.home()
    result: dict[str, dict[str, Any]] = {}
    for client in AGENT_CLIENTS:
        if not client.skill_dir:
            result[client.id] = {
                "client": client.id,
                "path": None,
                "count": 0,
                "items": [],
            }
            continue
        path = _resolve(client.skill_dir, home)
        items = []
        if path.exists() and path.is_dir():
            for child in sorted(path.iterdir(), key=lambda item: item.name.lower()):
                if child.name.startswith("."):
                    continue
                metadata = _read_skill_metadata(child)
                items.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "kind": "symlink"
                        if child.is_symlink()
                        else "dir"
                        if child.is_dir()
                        else "file",
                        "source": metadata.get("source"),
                        "mode": metadata.get("mode"),
                        "verified": (child / "SKILL.md").is_file(),
                    }
                )
        result[client.id] = {
            "client": client.id,
            "path": str(path),
            "count": len(items),
            "items": items[:30],
        }
    return result


def build_skill_install_shell(
    client_id: str, skill_name: str, source: str, *, windows: bool = False
) -> str | None:
    client = get_agent_client(client_id)
    safe_name = safe_skill_name(skill_name)
    source = source.strip()
    if client is None or not client.skill_dir or windows or not safe_name or not source:
        return None
    target = f"{client.skill_dir.rstrip('/')}/{safe_name}"
    if _looks_like_git_url(source):
        install_body = (
            'if [ -d "$target/.git" ]; then git -C "$target" pull --ff-only; '
            'else rm -rf "$target" && git clone "$source" "$target"; fi'
        )
    else:
        install_body = '[ -e "$source" ] || { echo "source not found: $source" >&2; exit 2; }; rm -rf "$target"; cp -a "$source" "$target"'
    return (
        _skill_shell_prefix(target, source)
        + install_body
        + '; printf "skill installed %s\\n" "$target"'
    )


def build_skill_update_shell(
    client_id: str, skill_name: str, *, windows: bool = False
) -> str | None:
    client = get_agent_client(client_id)
    safe_name = safe_skill_name(skill_name)
    if client is None or not client.skill_dir or windows or not safe_name:
        return None
    target = f"{client.skill_dir.rstrip('/')}/{safe_name}"
    return (
        _skill_shell_prefix(target, "")
        + '[ -d "$target" ] || { echo "skill not found: $target" >&2; exit 2; }; '
        '[ -d "$target/.git" ] || { echo "skill is not a git repo: $target"; exit 0; }; '
        'git -C "$target" pull --ff-only; printf "skill updated %s\\n" "$target"'
    )


def build_skill_delete_shell(
    client_id: str, skill_name: str, *, windows: bool = False
) -> str | None:
    client = get_agent_client(client_id)
    safe_name = safe_skill_name(skill_name)
    if client is None or not client.skill_dir or windows or not safe_name:
        return None
    target = f"{client.skill_dir.rstrip('/')}/{safe_name}"
    return (
        _skill_shell_prefix(target, "")
        + '[ -e "$target" ] || { echo "skill not found: $target"; exit 0; }; '
        'backup_root="$HOME/.wsl-ops-agent-backups/skills"; mkdir -p "$backup_root"; '
        'backup="$backup_root/$(basename "$target").$(date +%Y%m%d%H%M%S)"; '
        'mv "$target" "$backup"; printf "skill moved to backup %s\\n" "$backup"'
    )


def safe_skill_name(value: str) -> str:
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 128
        or candidate.startswith(".")
        or candidate in {".", ".."}
        or ".." in candidate
        or "/" in candidate
        or "\\" in candidate
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", candidate) is None
    ):
        return ""
    return candidate


def _resolve(value: str, home: Path) -> Path:
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _looks_like_git_url(value: str) -> bool:
    return value.startswith(("https://", "http://", "git@")) or value.endswith(".git")


def _skill_shell_prefix(target_path: str, source: str) -> str:
    quoted_target = _sq(target_path)
    quoted_source = _sq(source)
    return (
        f'p={quoted_target}; eval "target=$p"; '
        'case "$target" in /*) ;; *) target="$HOME/${target#~/}" ;; esac; '
        'mkdir -p "$(dirname "$target")"; '
        f"source={quoted_source}; "
    )


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


SKILL_METADATA_FILENAME = ".wsl-ops-skill.json"
SKILL_BACKUP_RELATIVE = Path(".wsl-ops-agent-backups") / "skills"


def install_skill_to_home(
    home: Path | str,
    client_id: str,
    skill_name: str,
    source: str,
    *,
    mode: str = "copy",
) -> dict[str, Any]:
    """Install one verified Skill into one client-specific local directory."""
    home_path = Path(home).expanduser()
    client = get_agent_client(client_id)
    safe_name = safe_skill_name(skill_name)
    mode = mode.strip().lower()
    if client is None or not client.skill_dir or "skills" not in client.write_support:
        raise ValueError(f"{client_id} 暂不支持 Skill 写入")
    if not safe_name:
        raise ValueError("skill_name is invalid")
    if mode not in {"copy", "symlink"}:
        raise ValueError("mode must be copy or symlink")
    source_value = source.strip()
    if not source_value:
        raise ValueError("source is required")
    if mode == "symlink" and _source_is_archive_or_remote(source_value):
        raise ValueError("symlink 只支持本地目录来源")

    target_root = _resolve(client.skill_dir, home_path)
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / safe_name
    existed = _path_exists(target)
    staging_parent = Path(tempfile.mkdtemp(prefix=f".{safe_name}.stage-", dir=target_root))
    materialized_cleanup: Path | None = None
    try:
        source_root, source_kind, materialized_cleanup = _materialize_skill_source(
            source_value, mode=mode, workspace=staging_parent
        )
        _validate_skill_root(source_root)
        metadata = {
            "schema_version": 1,
            "client_id": client_id,
            "skill_name": safe_name,
            "source": source_value,
            "source_kind": source_kind,
            "mode": mode,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        sidecar = _skill_sidecar(target)
        if mode == "symlink":
            staged = staging_parent / "skill-link"
            staged.symlink_to(source_root, target_is_directory=True)
            _replace_skill_target(
                target,
                staged,
                sidecar,
                metadata,
                mode=mode,
                backup_root=home_path / SKILL_BACKUP_RELATIVE,
            )
        else:
            staged = staging_parent / "skill-copy"
            shutil.copytree(source_root, staged, symlinks=False)
            _validate_skill_root(staged)
            (staged / SKILL_METADATA_FILENAME).write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            _replace_skill_target(
                target,
                staged,
                sidecar,
                metadata,
                mode=mode,
                backup_root=home_path / SKILL_BACKUP_RELATIVE,
            )
    finally:
        if materialized_cleanup is not None:
            shutil.rmtree(materialized_cleanup, ignore_errors=True)
        shutil.rmtree(staging_parent, ignore_errors=True)

    verified = _verify_skill_target(target, mode)
    if not verified:
        raise RuntimeError(f"Skill 回读校验失败: {target}")
    return {
        "client_id": client_id,
        "skill_name": safe_name,
        "path": str(target),
        "mode": mode,
        "source": source_value,
        "added": [] if existed else [safe_name],
        "updated": [safe_name] if existed else [],
        "removed": [],
        "verified": True,
    }


def update_skill_to_home(
    home: Path | str,
    client_id: str,
    skill_name: str,
    *,
    source: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    home_path = Path(home).expanduser()
    target = _skill_target(home_path, client_id, skill_name)
    metadata = _read_skill_metadata(target)
    source_value = (source or str(metadata.get("source") or "")).strip()
    if not source_value:
        raise ValueError("Skill 缺少来源元数据，请重新选择来源")
    selected_mode = (mode or str(metadata.get("mode") or "copy")).strip().lower()
    # Git 副本优先原地 fast-forward，避免更新时丢掉客户端目录权限和备份链。
    if (
        str(metadata.get("source_kind") or "") == "git"
        and selected_mode == "copy"
        and (target / ".git").is_dir()
    ):
        try:
            completed = subprocess.run(
                ["git", "-C", str(target), "pull", "--ff-only"],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"Git 更新失败: {exc}") from exc
        if completed.returncode != 0:
            raise ValueError((completed.stderr or completed.stdout or "Git 更新失败").strip())
        metadata["updated_at"] = datetime.now(UTC).isoformat()
        (target / SKILL_METADATA_FILENAME).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if not _verify_skill_target(target, selected_mode):
            raise RuntimeError(f"Skill 回读校验失败: {target}")
        return {
            "client_id": client_id,
            "skill_name": safe_skill_name(skill_name),
            "path": str(target),
            "mode": selected_mode,
            "source": source_value,
            "added": [],
            "updated": [safe_skill_name(skill_name)],
            "removed": [],
            "verified": True,
        }
    return install_skill_to_home(
        home_path, client_id, skill_name, source_value, mode=selected_mode
    )


def uninstall_skill_from_home(
    home: Path | str, client_id: str, skill_name: str
) -> dict[str, Any]:
    home_path = Path(home).expanduser()
    safe_name = safe_skill_name(skill_name)
    if not safe_name:
        raise ValueError("skill_name is invalid")
    target = _skill_target(home_path, client_id, safe_name)
    sidecar = _skill_sidecar(target)
    if not _path_exists(target):
        raise FileNotFoundError(f"Skill 不存在: {target}")
    backup_root = home_path / SKILL_BACKUP_RELATIVE
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    backup = backup_root / f"{safe_name}.{stamp}"
    shutil.move(str(target), str(backup))
    if sidecar.exists():
        shutil.move(str(sidecar), str(backup_root / f"{safe_name}.{stamp}.metadata.json"))
    verified = not _path_exists(target) and not sidecar.exists()
    return {
        "client_id": client_id,
        "skill_name": safe_name,
        "path": str(target),
        "backup_path": str(backup),
        "added": [],
        "updated": [],
        "removed": [safe_name],
        "verified": verified,
    }


def _skill_target(home: Path, client_id: str, skill_name: str) -> Path:
    client = get_agent_client(client_id)
    safe_name = safe_skill_name(skill_name)
    if client is None or not client.skill_dir or "skills" not in client.write_support:
        raise ValueError(f"{client_id} 暂不支持 Skill 写入")
    if not safe_name:
        raise ValueError("skill_name is invalid")
    return _resolve(client.skill_dir, home) / safe_name


def _skill_sidecar(target: Path) -> Path:
    return target.parent / f".{target.name}{SKILL_METADATA_FILENAME}"


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _replace_skill_target(
    target: Path,
    staged: Path,
    sidecar: Path,
    metadata: dict[str, Any],
    *,
    mode: str,
    backup_root: Path,
) -> None:
    if _path_exists(target):
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        shutil.move(str(target), str(backup_root / f"{target.name}.{stamp}"))
    if sidecar.exists():
        sidecar.unlink()
    os.replace(staged, target)
    if mode == "symlink":
        sidecar.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        sidecar.chmod(0o600)


def _verify_skill_target(target: Path, mode: str) -> bool:
    if not _path_exists(target) or not target.is_dir() or not (target / "SKILL.md").is_file():
        return False
    metadata = (
        _read_skill_metadata(target)
        if mode != "symlink"
        else _read_skill_metadata(target)
    )
    return bool(metadata)


def _read_skill_metadata(target: Path) -> dict[str, Any]:
    candidates = [target / SKILL_METADATA_FILENAME, _skill_sidecar(target)]
    for path in candidates:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                return value
    return {}


def _materialize_skill_source(
    source: str, *, mode: str, workspace: Path
) -> tuple[Path, str, Path | None]:
    path_source = source[7:] if source.startswith("file://") else source
    source_path = Path(path_source).expanduser()
    if source_path.exists():
        if source_path.is_dir():
            return source_path.resolve(), "directory", None
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            return _extract_skill_zip(source_path, workspace), "zip", workspace
        raise ValueError("Skill 来源必须是目录或 ZIP")
    if source.lower().endswith(".zip") and source.startswith(("http://", "https://")):
        archive = workspace / "source.zip"
        try:
            with urllib.request.urlopen(source, timeout=30) as response, archive.open("wb") as handle:
                shutil.copyfileobj(response, handle)
        except (OSError, urllib.error.URLError) as exc:
            raise ValueError(f"下载 Skill ZIP 失败: {exc}") from exc
        return _extract_skill_zip(archive, workspace), "zip", workspace
    if _looks_like_git_url(source) or source.startswith(("git://", "ssh://")):
        checkout = workspace / "git-checkout"
        try:
            completed = subprocess.run(
                ["git", "clone", "--depth", "1", source, str(checkout)],
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError(f"Git 克隆失败: {exc}") from exc
        if completed.returncode != 0:
            raise ValueError((completed.stderr or completed.stdout or "Git 克隆失败").strip())
        return _find_skill_root(checkout), "git", workspace
    raise ValueError("Skill 来源不存在，或不是目录、ZIP、Git URL")


def _extract_skill_zip(archive: Path, workspace: Path) -> Path:
    extract_root = workspace / "zip-extract"
    extract_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as handle:
            for info in handle.infolist():
                relative = PurePosixPath(info.filename)
                if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                    raise ValueError("ZIP 包含不安全路径")
                target = extract_root.joinpath(*relative.parts)
                if not target.resolve().is_relative_to(extract_root.resolve()):
                    raise ValueError("ZIP 路径越界")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                # Unix ZIP symlink 条目不能成为安装目录的一部分。
                if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                    raise ValueError("ZIP 不允许包含符号链接")
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(info) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
    except zipfile.BadZipFile as exc:
        raise ValueError("ZIP 文件无效") from exc
    return _find_skill_root(extract_root)


def _validate_skill_root(root: Path) -> None:
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise ValueError("Skill 来源缺少 SKILL.md")


def _find_skill_root(root: Path) -> Path:
    direct = root / "SKILL.md"
    if direct.is_file():
        return root
    matches = [path.parent for path in root.rglob("SKILL.md") if path.is_file()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError("Skill 来源缺少 SKILL.md")
    raise ValueError("Skill 来源包含多个 SKILL.md，无法确定入口")


def _source_is_archive_or_remote(source: str) -> bool:
    lowered = source.lower()
    return lowered.endswith(".zip") or lowered.startswith(("http://", "https://", "git@", "git://", "ssh://"))
