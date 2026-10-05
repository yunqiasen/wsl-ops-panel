"""Own the local Provider write/readback/commit lifecycle, shared by tasks and Profiles."""

from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any, Iterator, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from app.services.agent_native_lock import native_client_lock
from app.services.agent_paths import resolve_agent_paths
from app.services.agent_provider_adapters import (
    ADDITIVE_PROVIDER_APPS,
    PROVIDER_RESOURCE_SECTIONS,
    build_provider_form,
    read_provider_records,
)


_NATIVE_FILES = {
    "claude": ["settings.json"],
    "codex": ["config.toml", "auth.json"],
    "gemini": ["settings.json", ".env"],
    "grokbuild": ["config.toml"],
    "opencode": ["opencode.json"],
    "openclaw": ["openclaw.json"],
    "hermes": ["config.yaml"],
}


class ProviderProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_root: str
    home: str
    client_id: str
    provider_id: str
    operation: Literal["apply", "remove"] = "apply"
    provider: dict[str, Any]
    write_secrets: bool = True


def provider_matches(
    home: Path, provider: dict[str, Any], *, write_secrets: bool = True
) -> bool:
    client = str(provider["app_id"])
    records = read_provider_records(home, client)
    if client in ADDITIVE_PROVIDER_APPS:
        records = [r for r in records if r["provider_id"] == provider["id"]]
    if not records:
        return False
    settings = copy.deepcopy(provider.get("settings_config") or {})
    # Native snapshots win over legacy routing in the actual client writers.
    # Routing metadata is an upstream policy, not necessarily a native field.
    has_snapshot = {
        "codex": "config" in settings,
        "grokbuild": "config" in settings,
        "claude": isinstance(settings.get("config"), dict)
        or isinstance(settings.get("env"), dict)
        or "$schema" in settings,
        "gemini": isinstance(settings.get("config"), dict)
        or isinstance(settings.get("settings"), dict)
        or bool(settings.get("env")),
        "opencode": any(k in settings for k in ("config", "npm", "options", "models")),
        "openclaw": any(k in settings for k in ("config", "baseUrl", "api", "models")),
        "hermes": any(
            k in settings for k in ("config", "base_url", "api", "api_mode", "models")
        ),
    }[client]
    if has_snapshot:
        settings.pop("routing", None)
    if not _native_snapshot_matches(
        home, client, settings, records[0]["settings"], write_secrets
    ):
        return False
    expected = build_provider_form(client, settings)
    actual = build_provider_form(client, records[0]["settings"])
    if not write_secrets:
        expected, actual = _without_credentials(expected), _without_credentials(actual)
    # Router-only policy has no native projection; all represented client fields
    # (including model lists, headers, and credential when explicitly written) do.
    ignored = {"full_url", "use_outbound_proxy", "model_map"}
    if not write_secrets:
        ignored.update({"api_key", "auth_mode"})
    for key, value in expected.items():
        if key in ignored or value in (None, "", {}, []):
            continue
        if actual.get(key) != value:
            return False
    return True


def _native_snapshot_matches(
    home: Path, client: str, settings: dict, record: dict, write_secrets: bool
) -> bool:
    """Verify native fields not represented by the UI form, too."""
    root = resolve_agent_paths(client, home).root

    def read(name: str) -> str:
        path = root / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def matches(expected: Any, actual: Any) -> bool:
        sections = PROVIDER_RESOURCE_SECTIONS.get(client, set())
        if isinstance(expected, dict) and isinstance(actual, dict):
            expected = {k: v for k, v in expected.items() if k not in sections}
            actual = {k: v for k, v in actual.items() if k not in sections}
        if not write_secrets:
            expected, actual = (
                _without_credentials(expected),
                _without_credentials(actual),
            )
        return expected == actual

    if client in {"codex", "grokbuild"} and "config" in settings:
        if not matches(
            tomllib.loads(settings.get("config") or ""),
            tomllib.loads(read("config.toml")),
        ):
            return False
        if (
            client == "codex"
            and write_secrets
            and settings.get("write_auth") is not False
            and isinstance(settings.get("auth"), dict)
        ):
            return settings["auth"] == json.loads(read("auth.json") or "{}")
        return True
    if client == "hermes" and isinstance(settings.get("config"), str):
        return matches(
            yaml.safe_load(settings["config"]) or {},
            yaml.safe_load(read("config.yaml")) or {},
        )
    incoming = settings.get("config")
    if (
        client == "claude"
        and not isinstance(incoming, dict)
        and (isinstance(settings.get("env"), dict) or "$schema" in settings)
    ):
        incoming = {
            k: v
            for k, v in settings.items()
            if k not in {"type", "routing", "config_path"}
        }
    if client == "gemini" and not isinstance(incoming, dict):
        incoming = settings.get("settings")
    filename = _NATIVE_FILES[client][0]
    if isinstance(incoming, dict) and filename.endswith(".json"):
        actual = json.loads(read(filename) or "{}")
        if not matches(incoming, actual):
            return False
    if (
        client in ADDITIVE_PROVIDER_APPS
        and "config" not in settings
        and "native_provider" not in settings
    ):
        # Native readers may add a name; every submitted field must still match.
        expected = settings if write_secrets else _without_credentials(settings)
        actual = record if write_secrets else _without_credentials(record)
        return all(actual.get(k) == value for k, value in expected.items())
    return True


def _without_credentials(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _without_credentials(v)
            for k, v in value.items()
            if not any(
                marker in k.lower()
                for marker in (
                    "api_key",
                    "apikey",
                    "token",
                    "secret",
                    "password",
                    "auth",
                )
            )
        }
    if isinstance(value, list):
        return [_without_credentials(item) for item in value]
    return value


def _paths(projection: ProviderProjection) -> list[Path]:
    root = resolve_agent_paths(projection.client_id, Path(projection.home)).root
    names = _NATIVE_FILES[projection.client_id]
    return [root / name for name in names]


def _restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".provider-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def provider_projection(projection: ProviderProjection | None) -> Iterator[None]:
    if projection is None:
        yield
        return
    from app.services.agent_providers import AgentProviderStore
    from app.services.agent_router_config import AgentRouterConfigStore
    from app.services.agent_router_control import AgentRouterController

    root = Path(projection.data_root)
    with native_client_lock(root, projection.client_id):
        controller = AgentRouterController(
            AgentRouterConfigStore(root), home=Path(projection.home)
        )
        if projection.client_id in controller.active_takeover_clients():
            raise ValueError("Router 接管已改变，刷新后重新操作")
        store = AgentProviderStore(root)

        def check_resource() -> None:
            current = store.provider_for_apply(
                projection.client_id,
                projection.provider_id,
                include_secrets=projection.operation == "apply",
            )
            if current is None:
                raise ValueError("Provider 已从资源库移除")
            if any(
                current.get(k) != projection.provider.get(k)
                for k in ("settings_config", "meta")
            ):
                raise ValueError("Provider 配置已改变，刷新后重新操作")

        check_resource()
        paths = _paths(projection)
        if any(path.is_symlink() for path in paths):
            raise ValueError("Provider 配置路径是软链接，保留现场")
        before = {path: path.read_bytes() if path.exists() else None for path in paths}
        try:
            yield
            check_resource()
            if projection.operation == "remove":
                records = read_provider_records(
                    Path(projection.home), projection.client_id
                )
                if any(r["provider_id"] == projection.provider_id for r in records):
                    raise ValueError("Provider 移除回读失败")
            else:
                if not provider_matches(
                    Path(projection.home),
                    projection.provider,
                    write_secrets=projection.write_secrets,
                ):
                    raise ValueError("Provider 原生配置回读失败")
            for path in paths:
                if path.exists():
                    path.chmod(0o600)
            if projection.operation == "remove":
                store.clear_current(projection.client_id, projection.provider_id)
            elif not store.set_current(projection.client_id, projection.provider_id):
                raise ValueError("Provider 已从资源库移除")
        except BaseException:
            for path, content in before.items():
                _restore(path, content)
            raise
