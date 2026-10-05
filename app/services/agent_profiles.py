from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.services.agent_clients import get_agent_client
from app.services.state_store import PanelStateStore


PROFILE_RESOURCE_TYPES = {"provider", "mcp", "skill", "prompt", "router"}
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class AgentProfileStore:
    """Validated Profile metadata backed by the shared Agent SQLite database."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.state = PanelStateStore(self.data_root)

    def list(self) -> list[dict[str, Any]]:
        return self.state.list_profiles()

    def get(self, profile_id: str) -> dict[str, Any] | None:
        return self.state.get_profile(profile_id)

    def upsert(
        self,
        profile_id: str,
        name: str,
        *,
        description: str | None = None,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        clean_id = _profile_id(profile_id)
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("profile name is required")
        normalized = _normalize_items(items)
        return self.state.upsert_profile(
            clean_id,
            clean_name,
            str(description).strip() if description is not None else None,
            normalized,
        )

    def delete(self, profile_id: str) -> bool:
        return self.state.delete_profile(_profile_id(profile_id))

    def snapshot_client(self, profile_id: str, client_id: str) -> dict[str, Any]:
        profile = self.get(_profile_id(profile_id))
        if profile is None:
            raise FileNotFoundError(f"Profile 不存在: {profile_id}")
        clean_client = str(client_id).strip()
        if get_agent_client(clean_client) is None:
            raise ValueError(f"不支持的客户端: {clean_client}")
        return {
            **profile,
            "client_id": clean_client,
            "items": [
                dict(item)
                for item in profile.get("items", [])
                if item.get("client_id") == clean_client
            ],
        }


def _profile_id(value: str) -> str:
    clean = str(value).strip()
    if not clean or not _PROFILE_ID_RE.fullmatch(clean):
        raise ValueError("profile_id 只允许字母、数字、点、下划线和连字符")
    return clean


def _normalize_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    singular: set[tuple[str, str]] = set()
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise ValueError("profile item must be an object")
        client_id = str(raw.get("client_id") or "").strip()
        if get_agent_client(client_id) is None:
            raise ValueError(f"不支持的客户端: {client_id}")
        resource_type = str(raw.get("resource_type") or "").strip().lower()
        if resource_type not in PROFILE_RESOURCE_TYPES:
            raise ValueError(f"不支持的资源类型: {resource_type}")
        resource_id = str(raw.get("resource_id") or "").strip()
        if not resource_id or "\x00" in resource_id or len(resource_id) > 256:
            raise ValueError("resource_id is invalid")
        key = (client_id, resource_type, resource_id)
        if key in seen:
            raise ValueError(
                f"Profile 资源重复: {client_id}/{resource_type}/{resource_id}"
            )
        seen.add(key)
        if resource_type in {"provider", "prompt", "router"}:
            singular_key = (client_id, resource_type)
            if singular_key in singular:
                raise ValueError(
                    f"每个客户端只能配置一个 {resource_type} Profile 项"
                )
            singular.add(singular_key)
        config = raw.get("config")
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValueError("profile item config must be an object")
        _reject_secret_config(config)
        normalized.append(
            {
                "client_id": client_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "config": dict(config),
                "sort_index": int(raw.get("sort_index", index)),
            }
        )
    return normalized


def _reject_secret_config(value: Any, key_hint: str = "") -> None:
    lowered = key_hint.lower().replace("-", "_")
    markers = ("api_key", "apikey", "token", "secret", "password", "credential")
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_secret_config(child, str(key))
        return
    if isinstance(value, list):
        for child in value:
            _reject_secret_config(child, key_hint)
        return
    if isinstance(value, str) and value.strip():
        if any(marker in lowered for marker in markers):
            raise ValueError("Profile config 只保存资源引用，不保存密钥")
        if lowered in {"proxy", "outbound_proxy"} and "@" in value:
            raise ValueError("Profile config 不保存带认证信息的代理地址")
