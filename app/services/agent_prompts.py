from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from app.services.agent_paths import resolve_agent_paths

from app.services.state_store import PanelStateStore
from app.services.agent_clients import get_agent_client

PROMPT_STORE_FILENAME = 'prompts.json'


class AgentPromptStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / PROMPT_STORE_FILENAME
        self._state = PanelStateStore(self.data_root)
        self.state = self._state
        self._migrate_legacy_json_once()

    def list_prompts(self) -> dict[str, dict[str, Any]]:
        return self._state.list_prompts()

    def save_prompts(self, prompts: dict[str, dict[str, Any]]) -> None:
        for prompt_id, prompt in prompts.items():
            self._state.upsert_prompt(
                prompt_id,
                str(prompt.get("name") or prompt_id),
                str(prompt.get("content") or ""),
                description=(
                    str(prompt["description"])
                    if prompt.get("description") is not None
                    else None
                ),
            )

    def upsert_prompt(
        self,
        prompt_id: str,
        name: str,
        content: str,
        *,
        description: str | None = None,
    ) -> dict[str, Any]:
        return self._state.upsert_prompt(
            prompt_id, name, content, description=description
        )

    def get_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        return self.list_prompts().get(prompt_id)

    def delete_prompt(self, prompt_id: str) -> bool:
        return self._state.delete_prompt(prompt_id)

    def upsert_variant(
        self,
        prompt_id: str,
        client_id: str,
        platform: str,
        content: str,
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        if self.get_prompt(prompt_id) is None:
            raise FileNotFoundError(f"Prompt 资源不存在: {prompt_id}")
        return self._state.upsert_prompt_variant(
            prompt_id, client_id, platform, content, source=source
        )

    def list_variants(
        self,
        prompt_id: str | None = None,
        client_id: str | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._state.list_prompt_variants(prompt_id, client_id, platform)

    def resolve_content(
        self, prompt_id: str, client_id: str, platform: str = "linux"
    ) -> str:
        prompt = self.get_prompt(prompt_id)
        if prompt is None:
            raise FileNotFoundError(f"Prompt 资源不存在: {prompt_id}")
        variants = self.list_variants(prompt_id, client_id, platform)
        if not variants and platform != "any":
            variants = self.list_variants(prompt_id, client_id, "any")
        if variants:
            return str(variants[0]["content"])
        return str(prompt.get("content") or "")

    def import_current(
        self,
        home: Path | str,
        client_id: str,
        *,
        prompt_id: str | None = None,
        name: str | None = None,
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        manager = AgentPromptFileManager(
            home,
            self.data_root / "prompt-files",
            environ=environ,
            overrides=overrides,
        )
        observed = manager.import_current(client_id)
        if not observed.get("exists"):
            raise FileNotFoundError(f"{client_id} 当前 Prompt 文件不存在")
        resource_id = (prompt_id or f"{client_id}-current").strip()
        if not resource_id:
            raise ValueError("prompt_id is required")
        content = str(observed.get("content") or "")
        saved = self.upsert_prompt(resource_id, name or f"{client_id} 当前提示词", content)
        self.upsert_variant(
            resource_id, client_id, platform, content, source="import"
        )
        self._state.set_prompt_assignment(
            "__local__",
            client_id,
            resource_id,
            variant_client_id=client_id,
            variant_platform=platform,
        )
        self._state.clear_prompt_observations("__local__", client_id)
        self._state.upsert_prompt_observation(
            "__local__",
            client_id,
            resource_id,
            present=True,
            content_hash=str(observed.get("content_hash") or _content_hash(content)),
            status="installed",
        )
        return saved

    def refresh_client_observation(
        self,
        home: Path | str,
        client_id: str,
        *,
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any] | None:
        assignments = [
            item
            for item in self._state.list_prompt_assignments("__local__", client_id)
            if item.get("desired_enabled", True)
        ]
        self._state.clear_prompt_observations("__local__", client_id)
        if not assignments:
            return None
        prompt_id = str(assignments[0]["prompt_id"])
        manager = AgentPromptFileManager(
            home,
            self.data_root / "prompt-files",
            environ=environ,
            overrides=overrides,
        )
        try:
            observed = manager.import_current(client_id)
            expected = self.resolve_content(prompt_id, client_id, platform)
            present = bool(observed.get("exists"))
            content_hash = (
                str(observed.get("content_hash"))
                if observed.get("content_hash")
                else None
            )
            status = (
                "missing"
                if not present
                else "installed"
                if content_hash == _content_hash(expected)
                else "drifted"
            )
            error = None
        except (OSError, ValueError, FileNotFoundError) as exc:
            present = False
            content_hash = None
            status = "error"
            error = str(exc)
        self._state.upsert_prompt_observation(
            "__local__",
            client_id,
            prompt_id,
            present=present,
            content_hash=content_hash,
            status=status,
            error=error,
        )
        return {
            "prompt_id": prompt_id,
            "present": present,
            "content_hash": content_hash,
            "status": status,
            "error": error,
        }

    def install_local(
        self,
        home: Path | str,
        client_id: str,
        prompt_id: str,
        *,
        platform: str = "linux",
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        content = self.resolve_content(prompt_id, client_id, platform)
        manager = AgentPromptFileManager(
            home,
            self.data_root / "prompt-files",
            environ=environ,
            overrides=overrides,
        )
        result = manager.apply(client_id, content)
        self._state.set_prompt_assignment(
            "__local__",
            client_id,
            prompt_id,
            variant_client_id=client_id,
            variant_platform=platform,
        )
        self._state.clear_prompt_observations("__local__", client_id)
        self._state.upsert_prompt_observation(
            "__local__",
            client_id,
            prompt_id,
            present=True,
            content_hash=_content_hash(content),
            status="installed",
        )
        result["prompt_id"] = prompt_id
        return result

    def restore_local(
        self,
        home: Path | str,
        client_id: str,
        *,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> dict[str, Any]:
        manager = AgentPromptFileManager(
            home,
            self.data_root / "prompt-files",
            environ=environ,
            overrides=overrides,
        )
        result = manager.restore(client_id)
        self._state.remove_prompt_assignment("__local__", client_id)
        self._state.clear_prompt_observations("__local__", client_id)
        return result

    def _migrate_legacy_json_once(self) -> None:
        if not self.path.exists() or self._state.list_prompts():
            return
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            return
        prompts = payload.get('prompts', {}) if isinstance(payload, dict) else {}
        if isinstance(prompts, dict):
            self.save_prompts(prompts)


def build_prompt_apply_shell(client_id: str, content: str, *, windows: bool = False) -> str | None:
    client = get_agent_client(client_id)
    if client is None or not client.prompt_file or windows or 'prompts' not in client.write_support:
        return None
    path = client.prompt_file
    quoted_content = _sq(content.rstrip() + '\n')
    quoted_path = _sq(path)
    return (
        f'p={quoted_path}; eval "target=$p"; '
        'case "$target" in /*) ;; *) target="$HOME/${target#~/}" ;; esac; '
        'mkdir -p "$(dirname "$target")"; '
        'ts="$(date +%Y%m%d%H%M%S)"; [ -f "$target" ] && cp "$target" "$target.wsl-ops-agent-bak-$ts" || true; '
        f'printf "%s" {quoted_content} > "$target"; '
        'printf "applied prompt %s\\n" "$target"'
    )


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


class PromptFileConflictError(RuntimeError):
    """The managed prompt file changed after the last verified apply."""


class AgentPromptFileManager:
    def __init__(
        self,
        home: Path | str,
        state_root: Path | str,
        *,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> None:
        self.home = Path(home)
        self.environ = environ
        self.overrides = overrides
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_root.chmod(0o700)

    def import_current(self, client_id: str) -> dict[str, Any]:
        target = self._target(client_id)
        exists = target.is_file()
        content = target.read_text(encoding="utf-8") if exists else ""
        return {
            "client_id": client_id,
            "path": str(target),
            "exists": exists,
            "content": content,
            "content_hash": _content_hash(content) if exists else None,
        }

    def apply(self, client_id: str, content: str) -> dict[str, Any]:
        if not content.strip():
            raise ValueError("Prompt 内容为空，不会覆盖现有文件")
        target = self._target(client_id)
        state_path = self._state_path(client_id)
        state = self._read_state(state_path)
        if not state.get("active"):
            original_exists = target.is_file()
            original = target.read_bytes() if original_exists else b""
            backup = self._backup_path(client_id)
            if original_exists:
                _atomic_write_bytes(backup, original, mode=0o600)
            elif backup.exists():
                backup.unlink()
            state = {
                "schema_version": 1,
                "client_id": client_id,
                "target": str(target),
                "original_exists": original_exists,
                "original_hash": _bytes_hash(original) if original_exists else None,
                "backup_path": str(backup) if original_exists else None,
            }

        before = target.read_bytes() if target.is_file() else None
        payload = content.encode("utf-8")
        try:
            _atomic_write_bytes(target, payload, mode=0o600)
            readback = target.read_bytes()
            verified = readback == payload
            if not verified:
                raise RuntimeError(f"Prompt 回读校验失败: {target}")
        except Exception:
            if before is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(target, before, mode=0o600)
            raise

        state.update(
            {
                "active": True,
                "applied_hash": _bytes_hash(payload),
                "applied_at": _utc_now(),
            }
        )
        self._write_state(state_path, state)
        return {
            "client_id": client_id,
            "path": str(target),
            "content_hash": state["applied_hash"],
            "backup_path": state.get("backup_path"),
            "verified": True,
        }

    def restore(self, client_id: str) -> dict[str, Any]:
        target = self._target(client_id)
        state_path = self._state_path(client_id)
        state = self._read_state(state_path)
        if not state.get("active"):
            raise FileNotFoundError(f"{client_id} 没有可恢复的 Prompt 备份")
        current = target.read_bytes() if target.is_file() else b""
        if _bytes_hash(current) != state.get("applied_hash"):
            raise PromptFileConflictError("Prompt 文件已被外部修改，保留当前内容")

        if state.get("original_exists"):
            backup_path = Path(str(state.get("backup_path") or ""))
            if not backup_path.is_file():
                raise FileNotFoundError("Prompt 原始备份不存在")
            original = backup_path.read_bytes()
            if _bytes_hash(original) != state.get("original_hash"):
                raise RuntimeError("Prompt 原始备份哈希不匹配")
            _atomic_write_bytes(target, original, mode=0o600)
            verified = target.read_bytes() == original
        else:
            target.unlink(missing_ok=True)
            verified = not target.exists()
        if not verified:
            raise RuntimeError(f"Prompt 恢复回读失败: {target}")

        state.update({"active": False, "restored_at": _utc_now()})
        self._write_state(state_path, state)
        return {
            "client_id": client_id,
            "path": str(target),
            "restored": True,
            "verified": True,
        }

    def status(self, client_id: str) -> dict[str, Any]:
        state = self._read_state(self._state_path(client_id))
        return {
            "client_id": client_id,
            "active": bool(state.get("active")),
            "target": str(self._target(client_id)),
            "applied_hash": state.get("applied_hash"),
        }

    def _target(self, client_id: str) -> Path:
        client = get_agent_client(client_id)
        if (
            client is None
            or not client.prompt_file
            or "prompts" not in client.write_support
        ):
            raise ValueError(f"{client_id} 暂不支持 Prompt 写入")
        target = resolve_agent_paths(
            client_id,
            self.home,
            environ=self.environ,
            overrides=self.overrides,
        ).prompt
        if target is None:
            raise ValueError(f"{client_id} 暂不支持 Prompt 写入")
        return target

    def _state_path(self, client_id: str) -> Path:
        return self.state_root / f"{client_id}.json"

    def _backup_path(self, client_id: str) -> Path:
        return self.state_root / f"{client_id}.original.md"

    @staticmethod
    def _read_state(path: Path) -> dict[str, Any]:
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Prompt 状态文件无效: {path}") from exc
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _write_state(path: Path, value: dict[str, Any]) -> None:
        _atomic_write_bytes(
            path,
            (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            mode=0o600,
        )


def _atomic_write_bytes(path: Path, content: bytes, *, mode: int) -> None:
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        path.chmod(mode)
    finally:
        temporary.unlink(missing_ok=True)


def _content_hash(content: str) -> str:
    return _bytes_hash(content.encode("utf-8"))


def _bytes_hash(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
