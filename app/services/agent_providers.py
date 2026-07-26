from __future__ import annotations

import base64
import copy
import json
import tomllib
from pathlib import Path
from typing import Any

from app.services.agent_clients import AGENT_CLIENTS
from app.services.agent_provider_secrets import AgentProviderSecretStore
from app.services.state_store import PanelStateStore

SUPPORTED_PROVIDER_APPS = {client.id for client in AGENT_CLIENTS}
SECRET_KEYS = {"api_key", "apikey", "token", "secret", "password", "auth", "credential"}
REDACTED_SECRET = "••••••••"


class AgentProviderStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self._state = PanelStateStore(self.data_root)
        self._secrets = AgentProviderSecretStore(self.data_root)

    def list_providers(
        self, app_id: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        return self._state.list_agent_providers(app_id)

    def get_provider(self, app_id: str, provider_id: str) -> dict[str, Any] | None:
        return self._state.get_agent_provider(app_id, provider_id)

    def upsert_provider(
        self,
        *,
        app_id: str,
        provider_id: str,
        name: str,
        settings: dict[str, Any],
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
        return self._state.upsert_agent_provider(
            app_id=app_id,
            provider_id=provider_id,
            name=name,
            settings=settings,
            website_url=website_url,
            category=category,
            notes=notes,
            icon=icon,
            icon_color=icon_color,
            is_current=is_current,
            source=source,
        )

    def import_from_home(
        self, home: Path | str | None = None, apps: list[str] | None = None
    ) -> int:
        home_path = Path(home) if home is not None else Path.home()
        imported = import_providers_from_home(home_path, apps=apps)
        for provider in imported:
            self.upsert_provider(**provider)
        return len(imported)

    def set_current(self, app_id: str, provider_id: str) -> bool:
        return self._state.set_current_agent_provider(app_id, provider_id)

    def delete_provider(self, app_id: str, provider_id: str) -> bool:
        provider = self.get_provider(app_id, provider_id)
        deleted = self._state.delete_agent_provider(app_id, provider_id)
        if deleted and provider is not None:
            routing = dict(provider.get("settings_config", {}).get("routing") or {})
            self._secrets.delete(str(routing.get("secret_ref") or ""))
        return deleted

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
            return normalize_provider_profile(app_id, settings)
        except ValueError:
            return None

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
    home: Path, apps: list[str] | None = None
) -> list[dict[str, Any]]:
    selected_apps = set(apps or SUPPORTED_PROVIDER_APPS)
    providers: list[dict[str, Any]] = []
    for app_id, reader in {
        "codex": _read_codex_provider,
        "claude": _read_claude_provider,
        "gemini": _read_gemini_provider,
        "opencode": _read_opencode_provider,
        "openclaw": _read_openclaw_provider,
        "hermes": _read_hermes_provider,
    }.items():
        if app_id not in selected_apps:
            continue
        settings = reader(home)
        if not settings:
            continue
        providers.append(
            {
                "app_id": app_id,
                "provider_id": "local-current",
                "name": "当前本机配置",
                "settings": settings,
                "website_url": _default_website(app_id),
                "category": "local",
                "notes": "从当前设备配置导入。应用到客户端前会备份原文件。",
                "icon": app_id,
                "icon_color": _default_icon_color(app_id),
                "is_current": True,
                "source": "import",
            }
        )
    return providers


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
            if any(marker in lowered for marker in SECRET_KEYS):
                found = True

    walk(settings)
    return found


def redact_sensitive(value: Any, key_hint: str = "") -> Any:
    lowered = key_hint.lower()
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive(child, str(key)) for key, child in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(child, key_hint) for child in value]
    if (
        isinstance(value, str)
        and value
        and any(marker in lowered for marker in SECRET_KEYS)
    ):
        return REDACTED_SECRET
    return value


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
    safe = dict(provider)
    settings = dict(safe.get("settings_config") or {})
    safe["has_secrets"] = provider_has_secrets(settings)
    if include_settings:
        safe["settings_config"] = redact_sensitive(settings)
    else:
        safe.pop("settings_config", None)
    return safe


def _read_codex_provider(home: Path) -> dict[str, Any] | None:
    config_path = home / ".codex" / "config.toml"
    auth_path = home / ".codex" / "auth.json"
    if not config_path.exists() and not auth_path.exists():
        return None
    config_text = _read_text(config_path)
    auth_json = _read_json(auth_path) if auth_path.exists() else None
    settings: dict[str, Any] = {
        "type": "codex",
        "config_path": "~/.codex/config.toml",
        "config": config_text,
        "write_auth": False,
    }
    if auth_json is not None:
        settings["auth_path"] = "~/.codex/auth.json"
        settings["auth"] = auth_json
    settings.update(_codex_summary(config_text, auth_json))
    return settings


def _read_claude_provider(home: Path) -> dict[str, Any] | None:
    path = home / ".claude.json"
    payload = _read_json(path)
    if payload is None:
        return None
    return {"type": "claude", "config_path": "~/.claude.json", "config": payload}


def _read_gemini_provider(home: Path) -> dict[str, Any] | None:
    settings_path = home / ".gemini" / "settings.json"
    env_path = home / ".gemini" / ".env"
    settings = _read_json(settings_path) if settings_path.exists() else None
    env_text = _read_text(env_path) if env_path.exists() else ""
    if settings is None and not env_text:
        return None
    return {
        "type": "gemini",
        "settings_path": "~/.gemini/settings.json",
        "settings": settings or {},
        "env_path": "~/.gemini/.env",
        "env": env_text,
    }


def _read_opencode_provider(home: Path) -> dict[str, Any] | None:
    path = home / ".config" / "opencode" / "opencode.json"
    payload = _read_json(path)
    if payload is None:
        return None
    return {
        "type": "opencode",
        "config_path": "~/.config/opencode/opencode.json",
        "config": payload,
    }


def _read_openclaw_provider(home: Path) -> dict[str, Any] | None:
    path = home / ".openclaw" / "openclaw.json"
    payload = _read_json(path)
    if payload is None:
        return None
    return {
        "type": "openclaw",
        "config_path": "~/.openclaw/openclaw.json",
        "config": payload,
    }


def _read_hermes_provider(home: Path) -> dict[str, Any] | None:
    path = home / ".hermes" / "config.yaml"
    if not path.exists():
        return None
    return {
        "type": "hermes",
        "config_path": "~/.hermes/config.yaml",
        "config": _read_text(path),
    }


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


def _provider_apply_script(encoded_payload: str) -> str:
    return f"""
import base64, json, pathlib, shutil, time
payload = json.loads(base64.b64decode({encoded_payload!r}).decode('utf-8'))
provider = payload.get('provider') or {{}}
write_secrets = bool(payload.get('write_secrets'))
app_id = provider.get('app_id')
settings = provider.get('settings_config') or {{}}
home = pathlib.Path.home()
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

def redact_secret_lines(text):
    kept = []
    for line in str(text or '').splitlines():
        key = line.split(':', 1)[0].split('=', 1)[0].strip().lower()
        if any(marker in key for marker in secret_markers):
            continue
        kept.append(line)
    return '\\n'.join(kept).rstrip() + ('\\n' if kept else '')

if app_id == 'codex':
    write_text(home / '.codex' / 'config.toml', settings.get('config') or '')
    if write_secrets and settings.get('write_auth') is not False and isinstance(settings.get('auth'), dict):
        write_json(home / '.codex' / 'auth.json', settings.get('auth'))
elif app_id == 'claude':
    config = settings.get('config') or {{}}
    write_json(home / '.claude.json', config if write_secrets else without_secrets(config))
elif app_id == 'gemini':
    gemini_settings = settings.get('settings') or {{}}
    write_json(home / '.gemini' / 'settings.json', gemini_settings if write_secrets else without_secrets(gemini_settings))
    if write_secrets and settings.get('env'):
        write_text(home / '.gemini' / '.env', settings.get('env') or '')
elif app_id == 'opencode':
    config = settings.get('config') or {{}}
    write_json(home / '.config' / 'opencode' / 'opencode.json', config if write_secrets else without_secrets(config))
elif app_id == 'openclaw':
    config = settings.get('config') or {{}}
    write_json(home / '.openclaw' / 'openclaw.json', config if write_secrets else without_secrets(config))
elif app_id == 'hermes':
    config = settings.get('config') or ''
    write_text(home / '.hermes' / 'config.yaml', config if write_secrets else redact_secret_lines(config))
else:
    raise SystemExit('unsupported app_id: %s' % app_id)
print('provider switched', app_id, provider.get('id'))
""".strip()
