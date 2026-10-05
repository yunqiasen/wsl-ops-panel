from __future__ import annotations

import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

from app.services.agent_provider_profiles import normalize_provider_profile
from app.services.agent_providers import REDACTED_SECRET
from app.services.state_store import PanelStateStore


class AgentRouterConfigError(ValueError):
    """Raised when the local router configuration is invalid."""


_UNSET = object()


class AgentRouterConfigStore:
    """SQLite-backed runtime configuration for Agent Router."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "router.json"
        self._lock = RLock()
        self.state = PanelStateStore(self.data_root)
        self._migrate_legacy_json_once()

    def snapshot(self) -> dict[str, Any]:
        """Return the private snapshot for trusted router/control code."""
        with self._lock:
            return copy.deepcopy(self._read())

    def public_snapshot(self) -> dict[str, Any]:
        """Return a recursively redacted snapshot suitable for the web UI."""
        return _redact(self.snapshot())

    def update_global(
        self,
        *,
        listen_address: str | None = None,
        listen_port: int | None = None,
        show_home_switch: bool | None = None,
        outbound_proxy: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        with self._lock:
            payload = self._read()
            if listen_address is not None:
                address = str(listen_address).strip()
                if not address:
                    raise AgentRouterConfigError("listen address is required")
                payload["listen_address"] = address
            if listen_port is not None:
                port = _validate_port(listen_port)
                payload["listen_port"] = port
            if show_home_switch is not None:
                payload["show_home_switch"] = bool(show_home_switch)
            if outbound_proxy is not _UNSET:
                payload["outbound_proxy"] = (
                    None
                    if outbound_proxy is None
                    else _validate_proxy(str(outbound_proxy))
                )
            self._write(payload)
            return copy.deepcopy(payload)

    def set_provider(
        self,
        client_id: str,
        profile: dict[str, Any],
        *,
        provider_id: str | None = None,
    ) -> dict[str, Any]:
        client = _clean_id(client_id)
        if not client:
            raise AgentRouterConfigError("client id is required")
        try:
            normalized = normalize_provider_profile(client, dict(profile))
        except ValueError as exc:
            raise AgentRouterConfigError(str(exc)) from exc
        with self._lock:
            payload = self._read()
            providers = payload.setdefault("providers", {})
            providers[client] = normalized
            if provider_id is not None:
                payload.setdefault("provider_ids", {})[client] = _clean_id(provider_id)
            self._write(payload)
            return copy.deepcopy(normalized)

    def update_provider_policy(self, client_id: str, **changes: Any) -> dict[str, Any]:
        client = _clean_id(client_id)
        allowed = {"auto_failover", "max_retries", "failure_threshold", "cooldown_seconds"}
        unknown = set(changes) - allowed
        if unknown:
            raise AgentRouterConfigError("unsupported router policy field")
        with self._lock:
            payload = self._read()
            provider = payload.setdefault("providers", {}).get(client)
            if not isinstance(provider, dict):
                raise AgentRouterConfigError("router provider is not configured")
            updated = {**provider, **changes}
            normalized = normalize_provider_profile(client, updated)
            payload["providers"][client] = normalized
            self._write(payload)
            return copy.deepcopy(normalized)

    def get_provider(self, client_id: str) -> dict[str, Any] | None:
        value = self.snapshot().get("providers", {}).get(_clean_id(client_id))
        return copy.deepcopy(value) if isinstance(value, dict) else None

    def get_failover_queue(self, client_id: str) -> list[str]:
        client = _clean_id(client_id)
        queue = self.snapshot().get("failover_queues", {}).get(client, [])
        if not isinstance(queue, list):
            return []
        return [str(item) for item in queue if _clean_id(str(item))]

    def set_failover_queue(
        self, client_id: str, provider_ids: list[str]
    ) -> list[str]:
        client = _clean_id(client_id)
        if not client:
            raise AgentRouterConfigError("client id is required")
        ordered: list[str] = []
        seen: set[str] = set()
        for raw_provider_id in provider_ids:
            provider_id = _clean_id(str(raw_provider_id))
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            ordered.append(provider_id)
        with self._lock:
            payload = self._read()
            payload.setdefault("failover_queues", {})[client] = ordered
            self._write(payload)
        return list(ordered)

    def remove_provider_reference(self, client_id: str, provider_id: str) -> bool:
        client = _clean_id(client_id)
        provider = _clean_id(provider_id)
        if not client or not provider:
            return False
        with self._lock:
            payload = self._read()
            if payload.setdefault("provider_ids", {}).get(client) == provider:
                raise AgentRouterConfigError("current router provider cannot be deleted")
            changed = False
            queue = payload.setdefault("failover_queues", {}).get(client)
            if isinstance(queue, list):
                cleaned = [item for item in queue if str(item) != provider]
                if cleaned != queue:
                    payload["failover_queues"][client] = cleaned
                    changed = True
            profile = payload.setdefault("providers", {}).get(client)
            if isinstance(profile, dict):
                fallbacks = profile.get("fallbacks")
                if isinstance(fallbacks, list):
                    cleaned_fallbacks = [
                        item
                        for item in fallbacks
                        if not isinstance(item, dict)
                        or str(item.get("provider_id") or "") != provider
                    ]
                    if cleaned_fallbacks != fallbacks:
                        profile["fallbacks"] = cleaned_fallbacks
                        changed = True
            if changed:
                self._write(payload)
            return changed

    def remove_provider(self, client_id: str) -> bool:
        with self._lock:
            payload = self._read()
            providers = payload.setdefault("providers", {})
            existed = _clean_id(client_id) in providers
            providers.pop(_clean_id(client_id), None)
            provider_ids = payload.setdefault("provider_ids", {})
            provider_ids.pop(_clean_id(client_id), None)
            if existed:
                self._write(payload)
            return existed

    def set_takeover(self, client_id: str, enabled: bool) -> dict[str, bool]:
        client = _clean_id(client_id)
        if not client:
            raise AgentRouterConfigError("client id is required")
        with self._lock:
            payload = self._read()
            takeover = payload.setdefault("takeover", {})
            if enabled:
                takeover[client] = True
            else:
                takeover.pop(client, None)
            self._write(payload)
            return {
                str(key): bool(value)
                for key, value in takeover.items()
                if bool(value)
            }

    def _read(self) -> dict[str, Any]:
        return _with_defaults(self.state.get_router_snapshot("__local__"))

    def _write(self, payload: dict[str, Any]) -> None:
        self.state.replace_router_snapshot("__local__", _with_defaults(payload))

    def _migrate_legacy_json_once(self) -> None:
        marker = self.state.get_agent_setting("router_json_migrated_v1")
        if isinstance(marker, dict):
            return

        migrated = False
        source = "defaults"
        existing = self.state.get_router_snapshot("__local__")
        if existing:
            source = "sqlite"
        elif self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                loaded = None
            if isinstance(loaded, dict):
                self.state.replace_router_snapshot(
                    "__local__", _with_defaults(loaded)
                )
                migrated = True
                source = "router.json"
            try:
                self.path.chmod(0o600)
            except OSError:
                pass

        self.state.set_agent_setting(
            "router_json_migrated_v1",
            {
                "migrated": migrated,
                "source": source,
                "migrated_at": datetime.now(UTC).isoformat(),
            },
        )


def _with_defaults(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    result.setdefault("schema_version", 1)
    result.setdefault("listen_address", "127.0.0.1")
    result.setdefault("listen_port", 7888)
    result.setdefault("show_home_switch", True)
    result.setdefault("outbound_proxy", None)
    if not isinstance(result.get("takeover"), dict):
        result["takeover"] = {}
    if not isinstance(result.get("providers"), dict):
        result["providers"] = {}
    if not isinstance(result.get("provider_ids"), dict):
        result["provider_ids"] = {}
    if not isinstance(result.get("failover_queues"), dict):
        result["failover_queues"] = {}
    result.setdefault("updated_at", None)
    return result


def _validate_port(value: int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise AgentRouterConfigError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise AgentRouterConfigError("port must be between 1 and 65535")
    return port


def _validate_proxy(value: str) -> str:
    proxy = str(value).strip()
    parsed = urlparse(proxy)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.netloc:
        raise AgentRouterConfigError("proxy must use http, https, socks5, or socks5h")
    return proxy


def _clean_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "-", str(value).strip()).strip("-")


def _redact(value: Any, key_hint: str = "") -> Any:
    lowered = key_hint.lower()
    if isinstance(value, dict):
        return {str(key): _redact(child, str(key)) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact(child, key_hint) for child in value]
    if isinstance(value, str):
        if any(marker in lowered for marker in ("api_key", "apikey", "token", "secret", "password", "credential")):
            return REDACTED_SECRET
        if lowered in {"outbound_proxy", "proxy"} and "@" in value:
            parsed = urlparse(value)
            if parsed.hostname:
                return f"{parsed.scheme}://{parsed.hostname}:{parsed.port or ''}".rstrip(":")
    return value
