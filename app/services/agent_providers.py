from __future__ import annotations

import base64
import copy
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Mapping

import yaml

from app.services.agent_clients import AGENT_CLIENTS
from app.services.agent_provider_adapters import (
    ADDITIVE_PROVIDER_APPS,
    EXCLUSIVE_PROVIDER_APPS,
    build_provider_form,
    read_provider_records,
    summarize_provider,
)
from app.services.agent_provider_secrets import AgentProviderSecretStore
from app.services.agent_paths import resolve_agent_paths
from app.services.state_store import PanelStateStore

SUPPORTED_PROVIDER_APPS = {
    client.id for client in AGENT_CLIENTS if "providers" in client.write_support
}
SECRET_KEYS = {"api_key", "apikey", "token", "secret", "password", "auth", "credential"}
REDACTED_SECRET = "••••••••"


class CurrentProviderError(ValueError):
    pass


class AgentProviderStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self._state = PanelStateStore(self.data_root)
        self._secrets = AgentProviderSecretStore(self.data_root)
        self._migrate_legacy_secret_refs()

    def list_providers(
        self, app_id: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return {
            client_id: [self._enrich_provider(provider) for provider in providers]
            for client_id, providers in self._state.list_agent_providers(app_id).items()
        }

    def get_provider(self, app_id: str, provider_id: str) -> dict[str, Any] | None:
        provider = self._state.get_agent_provider(app_id, provider_id)
        return None if provider is None else self._enrich_provider(provider)

    @staticmethod
    def _enrich_provider(provider: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(provider)
        settings = dict(result.get("settings_config") or {})
        meta = dict(result.get("meta") or {})
        for key in (
            "native_source",
            "native_read_only",
            "official_mode",
            "config_error",
        ):
            if key in settings and key not in meta:
                meta[key] = settings[key]
        result["meta"] = meta
        result["summary"] = summarize_provider(
            str(result.get("app_id") or ""), settings, meta
        )
        return result

    def upsert_provider(
        self,
        *,
        app_id: str,
        provider_id: str,
        name: str,
        settings: dict[str, Any],
        meta: dict[str, Any] | None = None,
        website_url: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        icon: str | None = None,
        icon_color: str | None = None,
        is_current: bool = False,
        source: str = "manual",
    ) -> dict[str, Any]:
        if app_id not in SUPPORTED_PROVIDER_APPS:
            raise ValueError(f"unsupported app_id: {app_id}")
        existing = self.get_provider(app_id, provider_id)
        if existing is not None:
            settings = restore_redacted(
                settings, dict(existing.get("settings_config") or {})
            )
        settings = self._separate_routing_secrets(
            app_id, provider_id, settings, existing=existing
        )
        saved = self._state.upsert_agent_provider(
            app_id=app_id,
            provider_id=provider_id,
            name=name,
            settings=settings,
            meta=meta,
            website_url=website_url,
            category=category,
            notes=notes,
            icon=icon,
            icon_color=icon_color,
            is_current=is_current,
            source=source,
        )
        return self._enrich_provider(saved)

    def import_from_home(
        self,
        home: Path | str | None = None,
        apps: list[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
    ) -> int:
        home_path = Path(home) if home is not None else Path.home()
        selected_apps = sorted(set(apps or SUPPORTED_PROVIDER_APPS))
        imported_count = 0
        for app_id in selected_apps:
            if app_id not in SUPPORTED_PROVIDER_APPS:
                continue
            existing = self.list_providers(app_id).get(app_id, [])
            if app_id in EXCLUSIVE_PROVIDER_APPS and any(
                str(item.get("source") or "") == "manual" for item in existing
            ):
                continue
            for record in read_provider_records(
                home_path,
                app_id,
                environ=environ,
                overrides=overrides,
            ):
                current = self.get_provider(app_id, str(record["provider_id"]))
                if current is not None and str(current.get("source") or "") == "manual":
                    continue
                self.upsert_provider(
                    app_id=app_id,
                    provider_id=str(record["provider_id"]),
                    name=str(record.get("name") or record["provider_id"]),
                    settings=dict(record.get("settings") or {}),
                    meta=dict(record.get("meta") or {}),
                    website_url=_default_website(app_id),
                    category="official"
                    if record.get("meta", {}).get("official_mode")
                    else "local",
                    notes="从当前客户端原生配置导入。",
                    icon=app_id,
                    icon_color=_default_icon_color(app_id),
                    is_current=bool(record.get("is_current")),
                    source="import",
                )
                imported_count += 1
        return imported_count

    def set_current(self, app_id: str, provider_id: str) -> bool:
        return self._state.set_current_agent_provider(app_id, provider_id)

    def clear_current(self, app_id: str, provider_id: str) -> bool:
        return self._state.clear_current_agent_provider(app_id, provider_id)

    def duplicate_provider(
        self,
        app_id: str,
        provider_id: str,
        *,
        new_id: str | None = None,
        new_name: str | None = None,
    ) -> dict[str, Any]:
        source = self.provider_for_apply(app_id, provider_id, include_secrets=True)
        if source is None:
            raise KeyError("provider not found")
        existing_ids = {
            str(item.get("id") or "")
            for item in self.list_providers(app_id).get(app_id, [])
        }
        candidate = str(new_id or f"{provider_id}-copy").strip()
        if not candidate:
            candidate = f"{provider_id}-copy"
        base = candidate
        suffix = 2
        while candidate in existing_ids:
            candidate = f"{base}-{suffix}"
            suffix += 1
        meta = copy.deepcopy(dict(source.get("meta") or {}))
        if meta.get("native_read_only"):
            meta.pop("native_read_only", None)
            meta.pop("read_only_reason", None)
            meta["native_source"] = "copy"
        return self.upsert_provider(
            app_id=app_id,
            provider_id=candidate,
            name=str(new_name or f"{source.get('name') or provider_id} 副本"),
            settings=copy.deepcopy(dict(source.get("settings_config") or {})),
            meta=meta,
            website_url=source.get("website_url"),
            category=source.get("category"),
            notes=source.get("notes"),
            icon=source.get("icon"),
            icon_color=source.get("icon_color"),
            is_current=False,
            source="manual",
        )

    def live_provider_ids(
        self, app_id: str, home: Path | str | None = None
    ) -> set[str]:
        home_path = Path(home) if home is not None else Path.home()
        return {
            str(record.get("provider_id") or "")
            for record in read_provider_records(home_path, app_id)
            if str(record.get("provider_id") or "")
        }

    def delete_provider(self, app_id: str, provider_id: str) -> bool:
        provider = self.get_provider(app_id, provider_id)
        if provider is not None and bool(provider.get("is_current")):
            raise CurrentProviderError("current provider cannot be deleted")
        deleted = self._state.delete_agent_provider(app_id, provider_id)
        if deleted and provider is not None:
            routing = dict(provider.get("settings_config", {}).get("routing") or {})
            self._secrets.delete(str(routing.get("secret_ref") or ""))
        return deleted

    def provider_for_apply(
        self, app_id: str, provider_id: str, *, include_secrets: bool = False
    ) -> dict[str, Any] | None:
        """Return a write payload, hydrating private secrets only on explicit request."""
        provider = self.get_provider(app_id, provider_id)
        if provider is None:
            return None
        result = copy.deepcopy(provider)
        if not include_secrets:
            return result
        settings = result.get("settings_config")
        if not isinstance(settings, dict):
            return result
        routing = settings.get("routing")
        if not isinstance(routing, dict):
            return result
        secret_ref = str(routing.get("secret_ref") or "")
        secrets = self._secrets.resolve(secret_ref)
        if secrets:
            settings["routing"] = {**routing, **secrets}
        return result

    def runtime_profile(self, app_id: str, provider_id: str) -> dict[str, Any] | None:
        provider = self.get_provider(app_id, provider_id)
        if provider is None:
            return None
        from app.services.agent_provider_profiles import normalize_provider_profile

        settings = copy.deepcopy(dict(provider.get("settings_config") or {}))
        routing = settings.get("routing")
        if isinstance(routing, dict):
            secrets = self._secrets.resolve(str(routing.get("secret_ref") or ""))
            settings["routing"] = {**routing, **secrets}
        try:
            return normalize_provider_profile(
                app_id, settings, dict(provider.get("meta") or {})
            )
        except ValueError:
            return None

    def _migrate_legacy_secret_refs(self) -> None:
        setting_key = "agent_provider_local_current_secret_migration_v1"
        pending = self._state.get_agent_setting(setting_key, [])
        pending_apps = [str(app_id) for app_id in pending if str(app_id)]
        if not pending_apps:
            return
        with self._secrets._lock:
            payload = self._secrets._read()
            changed = False
            for app_id in pending_apps:
                old_ref = f"provider-secret:{app_id}:local-current"
                new_ref = f"provider-secret:{app_id}:default"
                if old_ref not in payload:
                    continue
                if new_ref in payload and payload[new_ref] != payload[old_ref]:
                    backup_ref = f"{new_ref}:pre-local-current-migration"
                    suffix = 2
                    while backup_ref in payload:
                        backup_ref = (
                            f"{new_ref}:pre-local-current-migration-{suffix}"
                        )
                        suffix += 1
                    payload[backup_ref] = payload[new_ref]
                payload[new_ref] = payload.pop(old_ref)
                changed = True
            if changed:
                self._secrets._write(payload)
        self._state.set_agent_setting(setting_key, [])

    def _separate_routing_secrets(
        self,
        app_id: str,
        provider_id: str,
        settings: dict[str, Any],
        *,
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        result = copy.deepcopy(settings)
        incoming = result.get("routing")
        if not isinstance(incoming, dict):
            return result
        routing = dict(incoming)
        existing_routing = (
            dict(existing.get("settings_config", {}).get("routing") or {})
            if existing is not None
            else {}
        )
        existing_ref = str(existing_routing.get("secret_ref") or "")
        existing_secrets = self._secrets.resolve(existing_ref)
        api_key = routing.pop("api_key", None)
        headers = routing.pop("headers", None)
        if api_key == REDACTED_SECRET:
            api_key = existing_secrets.get("api_key")
        if isinstance(headers, dict):
            old_headers = existing_secrets.get("headers")
            old_headers = old_headers if isinstance(old_headers, dict) else {}
            headers = {
                str(key): old_headers.get(key) if value == REDACTED_SECRET else value
                for key, value in headers.items()
            }
        elif headers is None:
            headers = existing_secrets.get("headers")
        secrets: dict[str, Any] = {}
        if api_key:
            secrets["api_key"] = str(api_key)
        if isinstance(headers, dict) and headers:
            secrets["headers"] = {str(key): str(value) for key, value in headers.items()}
        if secrets:
            routing["secret_ref"] = self._secrets.put(app_id, provider_id, secrets)
        elif existing_ref:
            routing["secret_ref"] = existing_ref
        result["routing"] = routing
        return result


def import_providers_from_home(
    home: Path,
    apps: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> list[dict[str, Any]]:
    selected_apps = set(apps or SUPPORTED_PROVIDER_APPS)
    providers: list[dict[str, Any]] = []
    for app_id, reader in {
        "codex": _read_codex_provider,
        "claude": _read_claude_provider,
        "gemini": _read_gemini_provider,
        "opencode": _read_opencode_provider,
        "openclaw": _read_openclaw_provider,
    }.items():
        if app_id not in selected_apps:
            continue
        settings = reader(home, environ=environ, overrides=overrides)
        if not settings:
            continue
        providers.append(
            _imported_provider_record(app_id, "local-current", "当前本机配置", settings)
        )

    if "grokbuild" in selected_apps:
        settings = _read_grokbuild_provider(
            home, environ=environ, overrides=overrides
        )
        if settings:
            providers.append(
                _imported_provider_record(
                    "grokbuild", "local-current", "当前本机配置", settings
                )
            )

    if "hermes" in selected_apps:
        providers.extend(
            _read_hermes_provider_records(
                home, environ=environ, overrides=overrides
            )
        )
    return providers


def _imported_provider_record(
    app_id: str,
    provider_id: str,
    name: str,
    settings: dict[str, Any],
    *,
    is_current: bool = True,
    native_read_only: bool = False,
) -> dict[str, Any]:
    return {
        "app_id": app_id,
        "provider_id": provider_id,
        "name": name,
        "settings": settings,
        "website_url": _default_website(app_id),
        "category": "local",
        "notes": "从当前设备配置导入。应用到客户端前会备份原文件。",
        "icon": app_id,
        "icon_color": _default_icon_color(app_id),
        "is_current": is_current,
        "source": "import",
    }


def build_provider_apply_shell(
    provider: dict[str, Any], *, windows: bool = False, write_secrets: bool = False
) -> str | None:
    if windows or not provider:
        return None
    app_id = str(provider.get("app_id") or "")
    if app_id not in SUPPORTED_PROVIDER_APPS:
        return None
    payload = {
        "provider": provider,
        "write_secrets": bool(write_secrets),
    }
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return "python3 - <<'PY'\n" + _provider_apply_script(encoded) + "\nPY"


def build_provider_remove_shell(
    app_id: str, provider_id: str, *, windows: bool = False
) -> str | None:
    if windows or app_id not in ADDITIVE_PROVIDER_APPS:
        return None
    encoded = base64.b64encode(
        json.dumps(
            {"app_id": app_id, "provider_id": provider_id},
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")
    return f"""python3 - <<'PY'
import base64, json, os, pathlib, shutil, tempfile, time
payload = json.loads(base64.b64decode({encoded!r}).decode('utf-8'))
app_id = payload['app_id']
provider_id = payload['provider_id']
home = pathlib.Path.home()
root_env = {{
    'opencode': 'OPENCODE_CONFIG_DIR',
    'openclaw': 'OPENCLAW_HOME',
    'hermes': 'HERMES_HOME',
}}
def root(client):
    raw = str(os.environ.get(root_env[client], '') or '').strip()
    path = pathlib.Path(raw).expanduser() if raw else {{
        'opencode': home / '.config' / 'opencode',
        'openclaw': home / '.openclaw',
        'hermes': home / '.hermes',
    }}[client]
    return path if path.is_absolute() else home / path
def backup(path):
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.wsl-ops-agent-bak-' + time.strftime('%Y%m%d%H%M%S')))
def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = pathlib.Path(handle.name)
    os.replace(temporary, path)
def read_json(path):
    if not path.exists():
        return {{}}
    value = json.loads(path.read_text(encoding='utf-8') or '{{}}')
    if not isinstance(value, dict):
        raise SystemExit('client config must be an object')
    return value
if app_id == 'opencode':
    path = root(app_id) / 'opencode.json'
    config = read_json(path)
    providers = config.get('provider')
    if isinstance(providers, dict):
        providers.pop(provider_id, None)
    atomic_text(path, json.dumps(config, ensure_ascii=False, indent=2) + '\\n')
elif app_id == 'openclaw':
    path = root(app_id) / 'openclaw.json'
    config = read_json(path)
    models = config.get('models')
    providers = models.get('providers') if isinstance(models, dict) else None
    if isinstance(providers, dict):
        providers.pop(provider_id, None)
    atomic_text(path, json.dumps(config, ensure_ascii=False, indent=2) + '\\n')
else:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('Hermes provider remove requires PyYAML') from exc
    path = root(app_id) / 'config.yaml'
    config = yaml.safe_load(path.read_text(encoding='utf-8') or '') or {{}} if path.exists() else {{}}
    if not isinstance(config, dict):
        raise SystemExit('Hermes config must be a mapping')
    providers = config.get('custom_providers')
    if isinstance(providers, list):
        config['custom_providers'] = [
            item for item in providers
            if not isinstance(item, dict) or str(item.get('name') or '') != provider_id
        ]
    atomic_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
print('provider removed', app_id, provider_id)
PY"""


def provider_has_secrets(settings: dict[str, Any]) -> bool:
    found = False

    def walk(value: Any, key_hint: str = "") -> None:
        nonlocal found
        if found:
            return
        lowered = key_hint.lower()
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, str(key))
        elif isinstance(value, list):
            for child in value:
                walk(child, key_hint)
        elif isinstance(value, str) and value.strip():
            if _is_secret_field(key_hint) or (
                lowered in {"config", "env"} and _config_text_has_secret(value)
            ):
                found = True

    walk(settings)
    return found


def _is_secret_field(key_hint: str) -> bool:
    snake_case = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key_hint))
    normalized = re.sub(r"[^a-z0-9]+", "_", snake_case.lower()).strip("_")
    if normalized in {
        "auth",
        "authorization",
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "credential",
        "credentials",
    }:
        return True
    return normalized.startswith(
        ("api_key_", "token_", "secret_", "password_", "credential_")
    ) or normalized.endswith(
        ("_api_key", "_apikey", "_token", "_secret", "_password", "_credential", "_credentials")
    )


def redact_sensitive(value: Any, key_hint: str = "") -> Any:
    lowered = key_hint.lower()
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive(child, str(key)) for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(child, key_hint) for child in value]
    if isinstance(value, str) and value and lowered in {"config", "env"}:
        return _redact_config_text(value)
    if (
        isinstance(value, str)
        and value
        and _is_secret_field(key_hint)
    ):
        return REDACTED_SECRET
    return value


def _config_text_has_secret(value: str) -> bool:
    return bool(
        re.search(
            r"(?im)^\s*(?:api[_-]?key|token|secret|password|authorization|auth[_-]?token)\s*[:=]",
            value,
        )
    )


def _redact_config_text(value: str) -> str:
    pattern = re.compile(
        r"(?im)^(\s*(?:[\"']?api[_-]?key[\"']?|[\"']?(?:token|secret|password|authorization|auth[_-]?token)[\"']?)\s*[:=]\s*)(.*?)(\s*(?:,|#.*)?$)"
    )

    def replace(match: re.Match[str]) -> str:
        prefix, _old, suffix = match.groups()
        return f"{prefix}{json.dumps(REDACTED_SECRET, ensure_ascii=False)}{suffix}"

    return pattern.sub(replace, value)


def restore_redacted(incoming: Any, existing: Any) -> Any:
    """保留前端脱敏占位符对应的原值，避免编辑时把密钥覆盖成圆点。"""
    if incoming == REDACTED_SECRET:
        return existing
    if isinstance(incoming, dict):
        old = existing if isinstance(existing, dict) else {}
        return {
            key: restore_redacted(value, old.get(key))
            for key, value in incoming.items()
        }
    if isinstance(incoming, list):
        old = existing if isinstance(existing, list) else []
        return [
            restore_redacted(value, old[index] if index < len(old) else None)
            for index, value in enumerate(incoming)
        ]
    return incoming


def public_provider(
    provider: dict[str, Any], *, include_settings: bool = False
) -> dict[str, Any]:
    safe = copy.deepcopy(provider)
    settings = dict(safe.get("settings_config") or {})
    meta = dict(safe.get("meta") or {})
    for key in ("native_source", "native_read_only", "official_mode", "config_error"):
        if key in settings and key not in meta:
            meta[key] = settings[key]
        if key in meta:
            safe[key] = meta[key]
    app_id = str(
        safe.get("app_id") or settings.get("type") or safe.get("id") or ""
    )
    summary = summarize_provider(app_id, settings, meta)
    safe["has_secrets"] = provider_has_secrets(settings)
    safe["meta"] = redact_sensitive(meta)
    safe["summary"] = summary
    if include_settings:
        safe["settings_config"] = redact_sensitive(settings)
        safe["form"] = redact_sensitive(
            build_provider_form(app_id, settings, meta)
        )
    else:
        safe.pop("settings_config", None)
        safe.pop("form", None)
    return safe


def _read_codex_provider(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, Any] | None:
    paths = resolve_agent_paths("codex", home, environ=environ, overrides=overrides)
    config_path = paths.config
    auth_path = paths.root / "auth.json"
    if not config_path.exists() and not auth_path.exists():
        return None
    config_text = _read_text(config_path)
    auth_json = _read_json(auth_path) if auth_path.exists() else None
    settings: dict[str, Any] = {
        "type": "codex",
        "config_path": str(config_path),
        "config": config_text,
        "write_auth": False,
    }
    if auth_json is not None:
        settings["auth_path"] = "~/.codex/auth.json"
        settings["auth"] = auth_json
    settings.update(_codex_summary(config_text, auth_json))
    return settings


def _read_claude_provider(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, Any] | None:
    path = resolve_agent_paths(
        "claude", home, environ=environ, overrides=overrides
    ).config
    payload = _read_json(path)
    if payload is None:
        return None
    return {"type": "claude", "config_path": str(path), "config": payload}


def _read_gemini_provider(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, Any] | None:
    paths = resolve_agent_paths("gemini", home, environ=environ, overrides=overrides)
    settings_path = paths.config
    env_path = paths.root / ".env"
    settings = _read_json(settings_path) if settings_path.exists() else None
    env_text = _read_text(env_path) if env_path.exists() else ""
    if settings is None and not env_text:
        return None
    return {
        "type": "gemini",
        "settings_path": str(settings_path),
        "settings": settings or {},
        "env_path": str(env_path),
        "env": env_text,
    }


def _read_opencode_provider(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, Any] | None:
    path = resolve_agent_paths(
        "opencode", home, environ=environ, overrides=overrides
    ).config
    payload = _read_json(path)
    if payload is None:
        return None
    return {
        "type": "opencode",
        "config_path": str(path),
        "config": payload,
    }


def _read_openclaw_provider(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, Any] | None:
    path = resolve_agent_paths(
        "openclaw", home, environ=environ, overrides=overrides
    ).config
    payload = _read_json(path)
    if payload is None:
        return None
    return {
        "type": "openclaw",
        "config_path": str(path),
        "config": payload,
    }


def _read_grokbuild_provider(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, Any] | None:
    path = resolve_agent_paths(
        "grokbuild", home, environ=environ, overrides=overrides
    ).config
    if not path.exists():
        return None
    config_text = _read_text(path)
    settings: dict[str, Any] = {
        "type": "grokbuild",
        "config_path": str(path),
        "config": config_text,
    }
    try:
        parsed = tomllib.loads(config_text or "")
    except tomllib.TOMLDecodeError as exc:
        settings["config_error"] = f"invalid_toml: {exc}"
        return settings
    if not isinstance(parsed, dict):
        settings["config_error"] = "config must be a TOML table"
        return settings

    models = parsed.get("models")
    model_tables = parsed.get("model")
    if not isinstance(models, dict) and not isinstance(model_tables, dict):
        # 官方 OAuth 状态可以是空 TOML，也可以只带 MCP/其它客户端字段。
        settings["official_mode"] = True
        return settings

    profile = models.get("default") if isinstance(models, dict) else None
    selected = (
        model_tables.get(str(profile))
        if isinstance(profile, str) and isinstance(model_tables, dict)
        else None
    )
    if not isinstance(selected, dict):
        settings["config_error"] = "custom_model_profile_missing"
        return settings

    base_url = _non_empty_string(selected.get("base_url"))
    upstream_model = _non_empty_string(selected.get("model"))
    if not profile or not base_url or not upstream_model:
        settings["config_error"] = "custom_model_profile_incomplete"
        return settings

    settings["official_mode"] = False
    settings["grok_profile"] = str(profile)
    settings["native_provider"] = copy.deepcopy(selected)
    routing: dict[str, Any] = {
        "base_url": base_url,
        "api_format": _grok_api_format(selected.get("api_backend")),
        "model": upstream_model,
    }
    inline_key = _non_empty_string(selected.get("api_key"))
    if inline_key:
        routing["api_key"] = inline_key
    settings["routing"] = routing
    return settings


def _read_hermes_provider_records(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> list[dict[str, Any]]:
    path = resolve_agent_paths(
        "hermes", home, environ=environ, overrides=overrides
    ).config
    if not path.exists():
        return []
    config_text = _read_text(path)
    base_settings: dict[str, Any] = {
        "type": "hermes",
        "config_path": str(path),
        "config": config_text,
    }
    try:
        parsed = yaml.safe_load(config_text or "") or {}
    except yaml.YAMLError as exc:
        settings = {**base_settings, "config_error": f"invalid_yaml: {exc}"}
        return [_imported_provider_record("hermes", "local-current", "当前本机配置", settings)]
    if not isinstance(parsed, dict):
        settings = {**base_settings, "config_error": "config must be a YAML mapping"}
        return [_imported_provider_record("hermes", "local-current", "当前本机配置", settings)]

    model_section = parsed.get("model")
    current_provider = (
        str(model_section.get("provider") or "").strip()
        if isinstance(model_section, dict)
        else ""
    )
    records: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    custom = parsed.get("custom_providers")
    if isinstance(custom, list):
        for entry in custom:
            if not isinstance(entry, dict):
                continue
            name = _non_empty_string(entry.get("name"))
            if not name:
                continue
            seen_names.add(name)
            settings = _hermes_provider_settings(
                base_settings, entry, native_source="custom_providers"
            )
            records.append(
                _imported_provider_record(
                    "hermes",
                    name,
                    name,
                    settings,
                    is_current=current_provider == name,
                )
            )

    provider_dict = parsed.get("providers")
    if isinstance(provider_dict, dict):
        for raw_key, raw_entry in provider_dict.items():
            if not isinstance(raw_entry, dict):
                continue
            key = _non_empty_string(raw_key)
            if not key:
                continue
            name = _non_empty_string(raw_entry.get("name")) or key
            if name in seen_names:
                continue
            seen_names.add(name)
            entry = dict(raw_entry)
            entry.setdefault("name", name)
            entry["provider_key"] = key
            settings = _hermes_provider_settings(
                base_settings,
                entry,
                native_source="providers_dict",
                native_read_only=True,
            )
            records.append(
                _imported_provider_record(
                    "hermes",
                    name,
                    name,
                    settings,
                    is_current=current_provider in {name, key},
                    native_read_only=True,
                )
            )

    if records:
        return records

    # 保留旧版只有顶层 model/base_url 的 Hermes 配置导入行为。
    fallback = dict(base_settings)
    if isinstance(model_section, dict):
        routing = _hermes_routing(model_section)
        if routing:
            fallback["routing"] = routing
    return [
        _imported_provider_record(
            "hermes", "local-current", "当前本机配置", fallback
        )
    ]


def _hermes_provider_settings(
    base: dict[str, Any],
    entry: dict[str, Any],
    *,
    native_source: str,
    native_read_only: bool = False,
) -> dict[str, Any]:
    settings = dict(base)
    settings["native_provider"] = copy.deepcopy(entry)
    settings["native_source"] = native_source
    if native_read_only:
        settings["native_read_only"] = True
    routing = _hermes_routing(entry)
    if routing:
        settings["routing"] = routing
    return settings


def _hermes_routing(entry: dict[str, Any]) -> dict[str, Any]:
    routing: dict[str, Any] = {}
    base_url = _non_empty_string(entry.get("base_url"))
    if base_url:
        routing["base_url"] = base_url
    api_mode = _non_empty_string(entry.get("api_mode"))
    if api_mode:
        routing["api_format"] = _hermes_api_format(api_mode)
    api_key = _non_empty_string(entry.get("api_key"))
    if api_key:
        routing["api_key"] = api_key
    model = _non_empty_string(entry.get("model"))
    if model:
        routing["model"] = model
    headers = entry.get("headers")
    if isinstance(headers, dict) and headers:
        routing["headers"] = copy.deepcopy(headers)
    auth_mode = _non_empty_string(entry.get("auth_mode"))
    if auth_mode:
        routing["auth_mode"] = auth_mode
    return routing


def _non_empty_string(value: Any) -> str:
    return str(value).strip() if value is not None and str(value).strip() else ""


def _grok_api_format(value: Any) -> str:
    normalized = _non_empty_string(value).lower().replace("-", "_")
    if normalized in {"chat", "chat_completions", "openai_chat"}:
        return "openai_chat"
    return "openai_responses"


def _hermes_api_format(value: Any) -> str:
    normalized = _non_empty_string(value).lower().replace("-", "_")
    if normalized in {"codex_responses", "responses", "openai_responses"}:
        return "openai_responses"
    if normalized in {"anthropic", "anthropic_messages"}:
        return "anthropic"
    if normalized == "gemini":
        return "gemini"
    return "openai_chat"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return None


def _codex_summary(config_text: str, auth_json: Any | None) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    try:
        parsed = tomllib.loads(config_text or "")
    except tomllib.TOMLDecodeError:
        parsed = {}
    if isinstance(parsed, dict):
        if parsed.get("model"):
            summary["model"] = parsed.get("model")
        if parsed.get("model_provider"):
            summary["model_provider"] = parsed.get("model_provider")
        provider_id = parsed.get("model_provider")
        providers = (
            parsed.get("model_providers")
            if isinstance(parsed.get("model_providers"), dict)
            else {}
        )
        if isinstance(provider_id, str) and isinstance(providers, dict):
            active = providers.get(provider_id)
            if isinstance(active, dict) and active.get("base_url"):
                summary["base_url"] = active.get("base_url")
    if isinstance(auth_json, dict) and auth_json.get("OPENAI_API_KEY"):
        summary["has_auth_api_key"] = True
    return summary


def _default_website(app_id: str) -> str | None:
    return {
        "codex": "https://github.com/openai/codex",
        "claude": "https://docs.anthropic.com/en/docs/claude-code",
        "gemini": "https://github.com/google-gemini/gemini-cli",
        "opencode": "https://opencode.ai",
        "openclaw": "https://github.com/qingchencloud/openclaw",
        "hermes": None,
    }.get(app_id)


def _default_icon_color(app_id: str) -> str | None:
    return {
        "codex": "#10b981",
        "claude": "#c2410c",
        "gemini": "#4f46e5",
        "opencode": "#4b5563",
        "openclaw": "#7c3aed",
        "hermes": "#be185d",
    }.get(app_id)



def _provider_snapshot_helpers() -> str:
    """Helpers for native TOML/YAML snapshots when credentials are excluded."""
    return r"""
def snapshot_secret_key(key):
    lowered = str(key).lower()
    return any(marker in lowered for marker in ('api_key', 'apikey', 'token', 'secret', 'password', 'credential', 'authorization')) or lowered in {'auth', 'headers', 'env'}

def strip_snapshot_secrets(value, key_hint=''):
    if isinstance(value, dict):
        return {
            str(key): strip_snapshot_secrets(child, str(key))
            for key, child in value.items()
            if not snapshot_secret_key(key)
        }
    if isinstance(value, list):
        return [strip_snapshot_secrets(child, key_hint) for child in value]
    if snapshot_secret_key(key_hint):
        return None
    return copy.deepcopy(value)

def snapshot_identity(value):
    if not isinstance(value, dict):
        return None
    for key in ('name', 'id', 'provider', 'key'):
        candidate = value.get(key)
        if candidate not in (None, ''):
            return (key, str(candidate))
    return None

def merge_snapshot_values(incoming, existing):
    # Merge a native snapshot without importing new credentials.
    if isinstance(incoming, dict):
        old = existing if isinstance(existing, dict) else {}
        # Keep target-only native extensions and siblings; incoming non-secret
        # fields then update them recursively.
        result = copy.deepcopy(old)
        for key, value in incoming.items():
            if snapshot_secret_key(key):
                if key in old:
                    result[key] = copy.deepcopy(old[key])
                else:
                    result.pop(key, None)
                continue
            result[key] = merge_snapshot_values(value, old.get(key))
        return result
    if isinstance(incoming, list):
        old = existing if isinstance(existing, list) else []
        result = copy.deepcopy(old)
        used = set()
        for index, value in enumerate(incoming):
            match = None
            identity = snapshot_identity(value)
            if identity is not None:
                match = next((
                    old_index for old_index, old_value in enumerate(old)
                    if old_index not in used and snapshot_identity(old_value) == identity
                ), None)
            if match is None and index < len(old) and index not in used:
                # Positional fallback is useful for anonymous native lists.
                match = index
            if match is not None:
                result[match] = merge_snapshot_values(value, old[match])
                used.add(match)
            else:
                result.append(strip_snapshot_secrets(value))
        return result
    return copy.deepcopy(incoming)

def toml_key(value):
    raw = str(value)
    return raw if raw.replace('-', '').replace('_', '').isalnum() else json.dumps(raw, ensure_ascii=False)

def toml_scalar(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_scalar(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join('%s = %s' % (toml_key(key), toml_scalar(item)) for key, item in value.items()) + '}'
    return json.dumps(str(value), ensure_ascii=False)

def toml_dump_table(table, prefix=()):
    if not isinstance(table, dict):
        return ''
    lines = []
    scalar_items = [(key, value) for key, value in table.items() if not isinstance(value, dict)]
    nested_items = [(key, value) for key, value in table.items() if isinstance(value, dict)]
    if prefix and (scalar_items or not nested_items):
        lines.append('[' + '.'.join(toml_key(part) for part in prefix) + ']')
    for key, value in scalar_items:
        lines.append('%s = %s' % (toml_key(key), toml_scalar(value)))
    for key, value in nested_items:
        if lines:
            lines.append('')
        lines.extend(toml_dump_table(value, (*prefix, str(key))).splitlines())
    return '\n'.join(lines)

def apply_grok_snapshot(path, incoming_text, write_secrets):
    import tomllib
    try:
        incoming = tomllib.loads(str(incoming_text or ''))
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit('Grok Build snapshot TOML parse failed: %s' % exc) from exc
    if not isinstance(incoming, dict):
        raise SystemExit('Grok Build snapshot must be a TOML table')
    if write_secrets:
        rendered = str(incoming_text or '')
    else:
        existing = {}
        if path.exists():
            try:
                loaded = tomllib.loads(path.read_text(encoding='utf-8') or '')
                existing = loaded if isinstance(loaded, dict) else {}
            except tomllib.TOMLDecodeError as exc:
                raise SystemExit('existing Grok Build config TOML parse failed: %s' % exc) from exc
        rendered = toml_dump_table(merge_snapshot_values(incoming, existing)).rstrip() + '\n'
    tomllib.loads(rendered or '')
    write_text(path, rendered)

def apply_hermes_snapshot(path, incoming_text, write_secrets):
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('Hermes snapshot apply requires PyYAML') from exc
    try:
        incoming = yaml.safe_load(str(incoming_text or '')) or {}
        existing = yaml.safe_load(path.read_text(encoding='utf-8') or '') or {} if path.exists() else {}
    except yaml.YAMLError as exc:
        raise SystemExit('Hermes snapshot YAML parse failed: %s' % exc) from exc
    if not isinstance(incoming, dict) or not isinstance(existing, dict):
        raise SystemExit('Hermes snapshot must be a YAML mapping')
    config = incoming if write_secrets else merge_snapshot_values(incoming, existing)
    rendered = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    if not isinstance(yaml.safe_load(rendered), dict):
        raise SystemExit('Hermes snapshot YAML validation failed')
    write_text(path, rendered)
""".strip()

def _provider_apply_script(encoded_payload: str) -> str:
    return f"""
import base64, copy, json, os, pathlib, pwd, shutil, time
payload = json.loads(base64.b64decode({encoded_payload!r}).decode('utf-8'))
provider = payload.get('provider') or {{}}
write_secrets = bool(payload.get('write_secrets'))
app_id = provider.get('app_id')
settings = provider.get('settings_config') or {{}}
routing = settings.get('routing') or {{}}
home = pathlib.Path.home()
root_env = {{
    'claude': 'CLAUDE_CONFIG_DIR',
    'codex': 'CODEX_HOME',
    'gemini': 'GEMINI_CLI_HOME',
    'grokbuild': 'GROK_HOME',
    'opencode': 'OPENCODE_CONFIG_DIR',
    'openclaw': 'OPENCLAW_HOME',
    'hermes': 'HERMES_HOME',
}}
default_roots = {{
    'claude': home / '.claude',
    'codex': home / '.codex',
    'gemini': home / '.gemini',
    'grokbuild': home / '.grok',
    'opencode': home / '.config' / 'opencode',
    'openclaw': home / '.openclaw',
    'hermes': home / '.hermes',
}}

default_rel = {{
    'claude': pathlib.Path('.claude'),
    'codex': pathlib.Path('.codex'),
    'gemini': pathlib.Path('.gemini'),
    'grokbuild': pathlib.Path('.grok'),
    'opencode': pathlib.Path('.config') / 'opencode',
    'openclaw': pathlib.Path('.openclaw'),
    'hermes': pathlib.Path('.hermes'),
}}
try:
    system_home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser()
except (KeyError, OSError):
    system_home = home

def client_env_root(client):
    raw = str(os.environ.get(root_env.get(client, ''), '') or '').strip()
    if not raw:
        return None
    path = pathlib.Path(raw).expanduser()
    path = path if path.is_absolute() else home / path
    # Test/fixture HOME values often inherit CODEX_HOME from the login shell.
    # Ignore only that stale default; genuine relocated roots remain honored.
    if home != system_home and path == system_home / default_rel[client]:
        return None
    return path

def client_root(client):
    return client_env_root(client) or default_roots[client]

def client_config(client, filename):
    override = client_env_root(client)
    if client == 'claude' and override is None:
        return home / '.claude' / filename
    return (override or default_roots[client]) / filename

secret_markers = ('api_key', 'apikey', 'token', 'secret', 'password', 'auth')

def backup(path):
    path = pathlib.Path(path)
    if path.exists():
        bak = path.with_name(path.name + '.wsl-ops-agent-bak-' + time.strftime('%Y%m%d%H%M%S'))
        shutil.copy2(path, bak)
        print('backup', bak)
    path.parent.mkdir(parents=True, exist_ok=True)

def write_text(path, content):
    backup(path)
    pathlib.Path(path).write_text(str(content or ''), encoding='utf-8')
    print('applied', path)

def write_json(path, content):
    backup(path)
    pathlib.Path(path).write_text(json.dumps(content if isinstance(content, (dict, list)) else {{}}, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
    print('applied', path)

def read_json(path):
    path = pathlib.Path(path)
    if not path.exists():
        return {{}}
    value = json.loads(path.read_text(encoding='utf-8') or '{{}}')
    if not isinstance(value, dict):
        raise SystemExit('client config must be a JSON object: %s' % path)
    return value

def update_env(path, updates):
    path = pathlib.Path(path)
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    pending = {{str(key): str(value) for key, value in updates.items() if value not in (None, '')}}
    rendered = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split('=', 1)[0].strip() if '=' in stripped and not stripped.startswith('#') else None
        if key in pending:
            rendered.append('%s=%s' % (key, pending.pop(key)))
        else:
            rendered.append(line)
    rendered.extend('%s=%s' % item for item in pending.items())
    write_text(path, '\\n'.join(rendered).rstrip() + ('\\n' if rendered else ''))

def without_secrets(value, key_hint=''):
    lowered = str(key_hint).lower()
    if isinstance(value, dict):
        return {{
            key: without_secrets(child, key)
            for key, child in value.items()
            if not any(marker in str(key).lower() for marker in secret_markers)
        }}
    if isinstance(value, list):
        return [without_secrets(child, key_hint) for child in value]
    if any(marker in lowered for marker in secret_markers):
        return None
    return value

def is_secret_key(key):
    lowered = str(key).lower()
    return any(marker in lowered for marker in secret_markers)

def merge_preserving_existing_secrets(incoming, existing):
    # Apply a snapshot while keeping credentials already owned by the target client.
    if isinstance(incoming, dict):
        old = existing if isinstance(existing, dict) else {{}}
        result = {{}}
        # Secret keys absent from the incoming snapshot still belong to the target.
        for key, value in old.items():
            if is_secret_key(key):
                result[key] = value
        for key, value in incoming.items():
            if is_secret_key(key):
                if key in old:
                    result[key] = old[key]
                continue
            result[key] = merge_preserving_existing_secrets(value, old.get(key))
        return result
    if isinstance(incoming, list):
        old = existing if isinstance(existing, list) else []
        return [
            merge_preserving_existing_secrets(value, old[index] if index < len(old) else None)
            for index, value in enumerate(incoming)
        ]
    return incoming

def redact_secret_lines(text):
    kept = []
    for line in str(text or '').splitlines():
        key = line.split(':', 1)[0].split('=', 1)[0].strip().lower()
        if any(marker in key for marker in secret_markers):
            continue
        kept.append(line)
    return '\\n'.join(kept).rstrip() + ('\\n' if kept else '')

{_provider_snapshot_helpers()}

def codex_provider_key(value):
    raw = str(value or 'wsl-agent-provider')
    cleaned = ''.join(char if (char.isalnum() or char in '-_') else '_' for char in raw)
    cleaned = cleaned.strip('-_') or 'wsl_agent_provider'
    if cleaned[0].isdigit():
        cleaned = 'provider_' + cleaned
    return cleaned.replace('-', '_')

def codex_set_top_level(text, updates):
    import re
    lines = str(text or '').splitlines()
    first_table = next((index for index, line in enumerate(lines) if line.strip().startswith('[')), len(lines))
    prefix = lines[:first_table]
    suffix = lines[first_table:]
    for key, value in updates.items():
        rendered = '%s = %s' % (key, json.dumps(str(value), ensure_ascii=False))
        pattern = re.compile(r'^\\s*' + re.escape(str(key)) + r'\\s*=')
        replaced = False
        for index, line in enumerate(prefix):
            if pattern.match(line):
                prefix[index] = rendered
                replaced = True
                break
        if not replaced:
            prefix.append(rendered)
    return '\\n'.join(prefix + suffix).rstrip() + '\\n'

def codex_replace_provider(text, provider_key, block):
    import re
    pattern = re.compile(
        r'(?ms)^\\[model_providers\\.' + re.escape(provider_key) + r'\\]\\s*\\n.*?(?=^\\[|\\Z)'
    )
    rendered = pattern.sub('', str(text or ''))
    return rendered.rstrip() + '\\n\\n' + block.rstrip() + '\\n'

def codex_provider_block(provider_key, provider_name, routing):
    api_format = str(routing.get('api_format') or 'openai_responses')
    wire_api = 'responses' if api_format == 'openai_responses' else 'chat'
    requires_auth = str(routing.get('auth_mode') or 'bearer') != 'none'
    newline = chr(10)
    return newline.join([
        '[model_providers.%s]' % provider_key,
        'name = %s' % json.dumps(str(provider_name or provider_key), ensure_ascii=False),
        'base_url = %s' % json.dumps(str(routing.get('base_url') or ''), ensure_ascii=False),
        'wire_api = %s' % json.dumps(wire_api),
        'requires_openai_auth = %s' % str(requires_auth).lower(),
    ]) + newline

def grok_provider_key(value):
    raw = str(value or 'relay')
    cleaned = ''.join(char if (char.isalnum() or char in '-_') else '_' for char in raw)
    cleaned = cleaned.strip('-_') or 'relay'
    if cleaned[0].isdigit():
        cleaned = 'provider_' + cleaned
    return cleaned.replace('-', '_')

def toml_update_table_field(text, header, key, value, remove=False):
    import re
    lines = str(text or '').splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == header), None)
    if start is None:
        if remove:
            return str(text or '')
        block = [header, '%s = %s' % (key, json.dumps(str(value), ensure_ascii=False))]
        return '\\n'.join(block + (lines if lines else [] )).rstrip() + '\\n'
    end = next((index for index in range(start + 1, len(lines)) if lines[index].strip().startswith('[')), len(lines))
    pattern = re.compile(r'^\\s*' + re.escape(str(key)) + r'\\s*=')
    matches = [index for index in range(start + 1, end) if pattern.match(lines[index])]
    if remove:
        for index in reversed(matches):
            lines.pop(index)
    else:
        rendered = '%s = %s' % (key, json.dumps(str(value), ensure_ascii=False))
        if matches:
            lines[matches[0]] = rendered
            for index in reversed(matches[1:]):
                lines.pop(index)
        else:
            lines.insert(end, rendered)
    return '\\n'.join(lines).rstrip() + '\\n'

def grok_api_backend(value):
    normalized = str(value or '').strip().lower().replace('-', '_')
    return 'chat_completions' if normalized in ('openai_chat', 'chat', 'chat_completions') else 'responses'

def grok_apply_routing(path, provider, settings, routing, write_secrets):
    import tomllib
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    try:
        parsed = tomllib.loads(text or '')
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit('Grok Build config.toml parse failed: %s' % exc) from exc
    profile = grok_provider_key(provider.get('id') or provider.get('name'))
    old_profile = parsed.get('models', {{}}).get('default') if isinstance(parsed.get('models'), dict) else None
    old_table = parsed.get('model', {{}}).get(old_profile, {{}}) if isinstance(parsed.get('model'), dict) and isinstance(old_profile, str) else {{}}
    existing_table = parsed.get('model', {{}}).get(profile, {{}}) if isinstance(parsed.get('model'), dict) else {{}}
    native = settings.get('native_provider') if isinstance(settings.get('native_provider'), dict) else {{}}
    model = routing.get('model') or native.get('model') or old_table.get('model') or profile
    base_url = routing.get('base_url') or native.get('base_url')
    if not base_url:
        raise SystemExit('Grok Build provider requires base_url')
    name = provider.get('name') or native.get('name') or profile
    backend = grok_api_backend(routing.get('api_format') or native.get('api_backend'))
    context_window = routing.get('context_window') or native.get('context_window') or old_table.get('context_window') or 500000
    rendered = toml_update_table_field(text, '[models]', 'default', profile)
    rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'model', model) if '[model.%s]' % profile in rendered else rendered
    if '[model.%s]' % profile not in rendered:
        rendered = rendered.rstrip() + '\\n\\n[model.%s]\\n' % profile
        rendered += 'model = %s\\n' % json.dumps(str(model), ensure_ascii=False)
    else:
        rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'model', model)
    rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'base_url', base_url)
    rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'name', name)
    rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'api_backend', backend)
    rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'context_window', context_window)
    api_key = routing.get('api_key')
    existing_key = existing_table.get('api_key') if isinstance(existing_table, dict) else None
    if write_secrets and api_key:
        rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'api_key', api_key)
    elif existing_key:
        rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'api_key', existing_key)
    else:
        rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'api_key', '', remove=True)
    env_key = native.get('env_key') or existing_table.get('env_key') or 'XAI_API_KEY'
    if env_key:
        rendered = toml_update_table_field(rendered, '[model.%s]' % profile, 'env_key', env_key)
    pathlib.Path(path).write_text(rendered, encoding='utf-8')
    try:
        checked = tomllib.loads(path.read_text(encoding='utf-8') or '')
        selected = checked.get('model', {{}}).get(profile, {{}})
        if checked.get('models', {{}}).get('default') != profile or selected.get('base_url') != base_url:
            raise ValueError('selected Grok profile readback mismatch')
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise SystemExit('Grok Build provider readback validation failed: %s' % exc) from exc

def hermes_api_mode(value):
    normalized = str(value or '').strip().lower().replace('-', '_')
    if normalized in ('openai_responses', 'responses', 'codex_responses'):
        return 'codex_responses'
    if normalized == 'anthropic':
        return 'anthropic'
    if normalized == 'gemini':
        return 'gemini'
    return 'chat_completions'

def hermes_apply_routing(path, provider, settings, routing, write_secrets):
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('Hermes provider apply requires PyYAML') from exc
    if settings.get('native_read_only'):
        raise SystemExit('Hermes providers dict entries are read-only')
    if path.exists():
        try:
            config = yaml.safe_load(path.read_text(encoding='utf-8') or '') or {{}}
        except yaml.YAMLError as exc:
            raise SystemExit('Hermes config.yaml parse failed: %s' % exc) from exc
    else:
        config = {{}}
    if not isinstance(config, dict):
        raise SystemExit('Hermes config must be a mapping')
    provider_id = str(provider.get('id') or provider.get('name') or 'relay').strip()
    native = settings.get('native_provider') if isinstance(settings.get('native_provider'), dict) else {{}}
    incoming = dict(native)
    incoming.pop('provider_key', None)
    incoming['name'] = provider_id
    base_url = routing.get('base_url') or incoming.get('base_url')
    if base_url:
        incoming['base_url'] = base_url
    api_format = routing.get('api_format') or incoming.get('api_mode')
    if api_format:
        incoming['api_mode'] = hermes_api_mode(api_format)
    model = routing.get('model') or incoming.get('model')
    if model:
        incoming['model'] = model
    if write_secrets and routing.get('api_key'):
        incoming['api_key'] = routing.get('api_key')
    elif not write_secrets:
        existing_entry = None
        current_list = config.get('custom_providers')
        if isinstance(current_list, list):
            existing_entry = next((item for item in current_list if isinstance(item, dict) and str(item.get('name') or '') == provider_id), None)
        if isinstance(existing_entry, dict) and existing_entry.get('api_key'):
            incoming['api_key'] = existing_entry['api_key']
        else:
            incoming.pop('api_key', None)
    if write_secrets and isinstance(routing.get('headers'), dict):
        incoming['headers'] = routing.get('headers')
    elif not write_secrets:
        incoming.pop('headers', None)
    current_list = config.get('custom_providers')
    providers = list(current_list) if isinstance(current_list, list) else []
    replaced = False
    for index, item in enumerate(providers):
        if isinstance(item, dict) and str(item.get('name') or '') == provider_id:
            merged = dict(item)
            merged.update(incoming)
            providers[index] = merged
            replaced = True
            break
    if not replaced:
        providers.append(incoming)
    config['custom_providers'] = providers
    original_model_section = config.get('model')
    if isinstance(original_model_section, dict):
        model_section = dict(original_model_section)
        model_section['provider'] = provider_id
        if model:
            model_section['default'] = model
        config['model'] = model_section
    else:
        # 兼容 Hermes 早期的顶层扁平配置，同时仍把可合并的 Provider
        # 写入 custom_providers，避免旧配置突然改变运行语义。
        if base_url:
            config['base_url'] = base_url
        config['api_mode'] = str(api_format or 'openai_chat')
        if model:
            config['model'] = model
    rendered = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
    checked = yaml.safe_load(rendered)
    if not isinstance(checked, dict):
        raise SystemExit('Hermes config validation failed')
    selected = [item for item in checked.get('custom_providers', []) if isinstance(item, dict) and item.get('name') == provider_id]
    if not selected or (base_url and selected[0].get('base_url') != base_url):
        raise SystemExit('Hermes provider readback validation failed')
    write_text(path, rendered)


if app_id == 'codex':
    if 'config' in settings:
        write_text(client_config('codex', 'config.toml'), settings.get('config') or '')
    else:
        path = client_config('codex', 'config.toml')
        config_text = path.read_text(encoding='utf-8') if path.exists() else ''
        provider_key = codex_provider_key(provider.get('id') or provider.get('name'))
        provider_name = provider.get('name') or provider_key
        updates = {{'model_provider': provider_key}}
        if routing.get('model'):
            updates['model'] = routing.get('model')
        config_text = codex_set_top_level(config_text, updates)
        block = codex_provider_block(provider_key, provider_name, routing)
        config_text = codex_replace_provider(config_text, provider_key, block)
        write_text(path, config_text)
        if write_secrets and routing.get('api_key'):
            auth_path = client_root('codex') / 'auth.json'
            auth = read_json(auth_path)
            auth['OPENAI_API_KEY'] = routing.get('api_key')
            write_json(auth_path, auth)
    if write_secrets and settings.get('write_auth') is not False and isinstance(settings.get('auth'), dict):
        write_json(client_root('codex') / 'auth.json', settings.get('auth'))
elif app_id == 'claude':
    path = client_config('claude', 'settings.json')
    if isinstance(settings.get('config'), dict):
        incoming = settings.get('config') or {{}}
    elif isinstance(settings.get('env'), dict) or '$schema' in settings:
        incoming = {{key: value for key, value in settings.items() if key not in ('type', 'routing', 'config_path')}}
    else:
        incoming = None
    if incoming is not None:
        config = incoming if write_secrets else merge_preserving_existing_secrets(incoming, read_json(path))
    else:
        config = read_json(path)
        env = config.get('env') or {{}}
        env = dict(env) if isinstance(env, dict) else {{}}
        if routing.get('base_url'):
            env['ANTHROPIC_BASE_URL'] = routing.get('base_url')
        if routing.get('model'):
            env['ANTHROPIC_MODEL'] = routing.get('model')
        if write_secrets and routing.get('api_key'):
            key = 'ANTHROPIC_AUTH_TOKEN' if routing.get('auth_mode') == 'bearer' else 'ANTHROPIC_API_KEY'
            env[key] = routing.get('api_key')
        config['env'] = env
    write_json(path, config)
elif app_id == 'gemini':
    settings_path = client_config('gemini', 'settings.json')
    env_path = client_root('gemini') / '.env'
    native_config = settings.get('config') if isinstance(settings.get('config'), dict) else settings.get('settings')
    if isinstance(native_config, dict):
        current = read_json(settings_path)
        gemini_settings = native_config if write_secrets else merge_preserving_existing_secrets(native_config, current)
        write_json(settings_path, gemini_settings)
    native_env = settings.get('env')
    if isinstance(native_env, dict):
        updates = {{
            key: value for key, value in native_env.items()
            if write_secrets or not is_secret_key(key)
        }}
        update_env(env_path, updates)
    elif write_secrets and native_env:
        write_text(env_path, native_env)
    elif native_config is None:
        updates = {{
            'GOOGLE_GEMINI_BASE_URL': routing.get('base_url'),
            'GEMINI_MODEL': routing.get('model'),
        }}
        if write_secrets and routing.get('api_key'):
            updates['GEMINI_API_KEY'] = routing.get('api_key')
        update_env(env_path, updates)
elif app_id == 'opencode':
    path = client_config('opencode', 'opencode.json')
    if isinstance(settings.get('config'), dict):
        incoming = settings.get('config') or {{}}
        config = incoming if write_secrets else merge_preserving_existing_secrets(incoming, read_json(path))
    elif any(key in settings for key in ('npm', 'options', 'models')):
        config = read_json(path)
        providers = config.get('provider') if isinstance(config.get('provider'), dict) else {{}}
        fragment = copy.deepcopy(settings)
        if not write_secrets:
            old = providers.get(str(provider.get('id') or ''), {{}})
            fragment = merge_preserving_existing_secrets(fragment, old)
        providers[str(provider.get('id') or provider.get('name'))] = fragment
        config['provider'] = providers
    else:
        config = read_json(path)
        options = config.get('options') or {{}}
        options = dict(options) if isinstance(options, dict) else {{}}
        if routing.get('base_url'):
            options['baseURL'] = routing.get('base_url')
        if write_secrets and routing.get('api_key'):
            options['apiKey'] = routing.get('api_key')
        config['options'] = options
        if routing.get('model'):
            config['model'] = routing.get('model')
    write_json(path, config)
elif app_id == 'openclaw':
    path = client_config('openclaw', 'openclaw.json')
    if isinstance(settings.get('config'), dict):
        incoming = settings.get('config') or {{}}
        config = incoming if write_secrets else merge_preserving_existing_secrets(incoming, read_json(path))
    elif any(key in settings for key in ('baseUrl', 'api', 'models')):
        config = read_json(path)
        model_section = config.get('models') if isinstance(config.get('models'), dict) else {{}}
        providers = model_section.get('providers') if isinstance(model_section.get('providers'), dict) else {{}}
        fragment = copy.deepcopy(settings)
        if not write_secrets:
            old = providers.get(str(provider.get('id') or ''), {{}})
            fragment = merge_preserving_existing_secrets(fragment, old)
        providers[str(provider.get('id') or provider.get('name'))] = fragment
        model_section['providers'] = providers
        model_section.setdefault('mode', 'merge')
        config['models'] = model_section
    else:
        config = read_json(path)
        if routing.get('base_url'):
            config['baseUrl'] = routing.get('base_url')
        if routing.get('model'):
            config['model'] = routing.get('model')
        if write_secrets and routing.get('api_key'):
            config['apiKey'] = routing.get('api_key')
    write_json(path, config)
elif app_id == 'grokbuild':
    path = client_config('grokbuild', 'config.toml')
    if 'config' in settings:
        apply_grok_snapshot(path, settings.get('config') or '', write_secrets)
    else:
        backup(path)
        grok_apply_routing(path, provider, settings, routing, write_secrets)
        print('applied', path)
elif app_id == 'hermes':
    path = client_config('hermes', 'config.yaml')
    if isinstance(settings.get('config'), str):
        apply_hermes_snapshot(path, settings.get('config') or '', write_secrets)
    elif any(key in settings for key in ('base_url', 'api', 'api_mode', 'models')):
        try:
            import yaml
        except ImportError as exc:
            raise SystemExit('Hermes provider apply requires PyYAML') from exc
        config = yaml.safe_load(path.read_text(encoding='utf-8') or '') or {{}} if path.exists() else {{}}
        if not isinstance(config, dict):
            raise SystemExit('Hermes config must be a mapping')
        providers = list(config.get('custom_providers') or []) if isinstance(config.get('custom_providers'), list) else []
        provider_id = str(provider.get('id') or provider.get('name') or 'relay')
        fragment = copy.deepcopy(settings)
        fragment['name'] = provider_id
        replaced = False
        for index, item in enumerate(providers):
            if isinstance(item, dict) and str(item.get('name') or '') == provider_id:
                fragment = fragment if write_secrets else merge_preserving_existing_secrets(fragment, item)
                providers[index] = fragment
                replaced = True
                break
        if not replaced:
            providers.append(fragment if write_secrets else without_secrets(fragment))
        config['custom_providers'] = providers
        write_text(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    else:
        backup(path)
        hermes_apply_routing(path, provider, settings, routing, write_secrets)
        print('applied', path)
else:
    raise SystemExit('unsupported app_id: %s' % app_id)
print('provider switched', app_id, provider.get('id'))
""".strip()
