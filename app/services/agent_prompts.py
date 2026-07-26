from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.state_store import PanelStateStore
from app.services.agent_clients import get_agent_client

PROMPT_STORE_FILENAME = 'prompts.json'


class AgentPromptStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / PROMPT_STORE_FILENAME
        self._state = PanelStateStore(self.data_root)
        self._migrate_legacy_json_once()

    def list_prompts(self) -> dict[str, dict[str, Any]]:
        return self._state.list_prompts()

    def save_prompts(self, prompts: dict[str, dict[str, Any]]) -> None:
        for prompt_id, prompt in prompts.items():
            self._state.upsert_prompt(prompt_id, str(prompt.get('name') or prompt_id), str(prompt.get('content') or ''))

    def upsert_prompt(self, prompt_id: str, name: str, content: str) -> dict[str, Any]:
        return self._state.upsert_prompt(prompt_id, name, content)

    def get_prompt(self, prompt_id: str) -> dict[str, Any] | None:
        return self.list_prompts().get(prompt_id)

    def delete_prompt(self, prompt_id: str) -> bool:
        return self._state.delete_prompt(prompt_id)

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
    def __init__(self, home: Path | str, state_root: Path | str) -> None:
        self.home = Path(home)
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
        if client.prompt_file.startswith("~/"):
            return self.home / client.prompt_file[2:]
        return Path(client.prompt_file)

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
