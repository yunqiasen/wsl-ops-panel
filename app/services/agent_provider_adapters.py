from __future__ import annotations

import copy
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from app.services.agent_paths import resolve_agent_paths

EXCLUSIVE_PROVIDER_APPS = {"claude", "codex", "gemini", "grokbuild"}
ADDITIVE_PROVIDER_APPS = {"opencode", "openclaw", "hermes"}
SUPPORTED_PROVIDER_APPS = EXCLUSIVE_PROVIDER_APPS | ADDITIVE_PROVIDER_APPS

_API_FORMAT_ALIASES = {
    "anthropic": "anthropic",
    "anthropic_messages": "anthropic",
    "messages": "anthropic",
    "openai": "openai_chat",
    "openai_chat": "openai_chat",
    "openai-completions": "openai_chat",
    "openai_completions": "openai_chat",
    "chat": "openai_chat",
    "chat_completions": "openai_chat",
    "chat-completions": "openai_chat",
    "openai_responses": "openai_responses",
    "openai-responses": "openai_responses",
    "responses": "openai_responses",
    "codex_responses": "openai_responses",
    "gemini": "gemini",
    "gemini_native": "gemini",
    "google": "gemini",
}
_DEFAULT_API_FORMAT = {
    "claude": "anthropic",
    "codex": "openai_responses",
    "gemini": "gemini",
    "grokbuild": "openai_responses",
    "opencode": "openai_chat",
    "openclaw": "openai_chat",
    "hermes": "openai_chat",
}
_SECRET_MARKERS = ("api_key", "apikey", "token", "secret", "password", "credential")


class ProviderAdapterError(ValueError):
    pass


def provider_mode(app_id: str) -> str:
    _validate_app(app_id)
    return "exclusive" if app_id in EXCLUSIVE_PROVIDER_APPS else "additive"


def summarize_provider(
    app_id: str,
    settings: Mapping[str, Any] | None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    form = build_provider_form(app_id, settings or {}, meta or {})
    metadata = dict(meta or {})
    read_only_reason = str(metadata.get("read_only_reason") or "").strip()
    if metadata.get("native_read_only") and not read_only_reason:
        read_only_reason = "Hermes 原生 providers 配置由客户端维护"
    official_mode = bool(metadata.get("official_mode"))
    if official_mode and not read_only_reason:
        read_only_reason = "官方登录配置由客户端认证流程维护"
    models = _form_models(form)
    model = _text(form.get("model")) or (models[0] if models else "")
    has_credentials = _has_credentials(settings or {}) or bool(
        _text(form.get("api_key")) or _text(form.get("env_key"))
    )
    return {
        "base_url": _text(form.get("base_url")),
        "model": model,
        "models": models,
        "api_format": _normalize_api_format(form.get("api_format"), app_id),
        "auth_mode": _text(form.get("auth_mode")) or _default_auth_mode(app_id),
        "has_credentials": has_credentials,
        "editable": not bool(metadata.get("native_read_only") or official_mode),
        "read_only_reason": read_only_reason or None,
        "native_mode": _text(metadata.get("native_source")) or provider_mode(app_id),
        "mode": provider_mode(app_id),
        "official_mode": official_mode,
    }


def build_provider_form(
    app_id: str,
    settings: Mapping[str, Any] | None,
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_app(app_id)
    native = copy.deepcopy(dict(settings or {}))
    metadata = dict(meta or {})
    form: dict[str, Any]

    if app_id == "claude":
        config = _claude_settings(native)
        env = _dict(config.get("env"))
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        form = {
            "base_url": _text(env.get("ANTHROPIC_BASE_URL")),
            "api_key": _text(token or env.get("ANTHROPIC_API_KEY")),
            "auth_mode": "bearer" if token else "x-api-key",
            "api_format": metadata.get("api_format") or "anthropic",
            "model": _text(env.get("ANTHROPIC_MODEL") or config.get("model")),
            "haiku_model": _text(env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")),
            "sonnet_model": _text(env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")),
            "opus_model": _text(env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")),
        }
    elif app_id == "codex":
        config_text = _text(native.get("config"))
        parsed = _toml_load(config_text)
        provider_key = _text(parsed.get("model_provider"))
        provider_table = _dict(_dict(parsed.get("model_providers")).get(provider_key))
        auth = _dict(native.get("auth"))
        form = {
            "provider_key": provider_key,
            "base_url": _text(provider_table.get("base_url") or native.get("base_url")),
            "api_key": _text(auth.get("OPENAI_API_KEY")),
            "auth_mode": "bearer" if provider_table.get("requires_openai_auth") is not False else "none",
            "api_format": metadata.get("api_format")
            or _wire_api_format(provider_table.get("wire_api"), "codex"),
            "model": _text(parsed.get("model") or native.get("model")),
        }
    elif app_id == "gemini":
        env = _gemini_env(native.get("env"))
        form = {
            "base_url": _text(
                env.get("GOOGLE_GEMINI_BASE_URL")
                or env.get("GEMINI_BASE_URL")
                or env.get("GOOGLE_API_BASE_URL")
            ),
            "api_key": _text(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY")),
            "auth_mode": "query",
            "api_format": "gemini",
            "model": _text(env.get("GEMINI_MODEL")),
        }
    elif app_id == "grokbuild":
        config_text = _text(native.get("config"))
        parsed = _toml_load(config_text)
        profile = _text(_dict(parsed.get("models")).get("default"))
        table = _dict(_dict(parsed.get("model")).get(profile))
        form = {
            "profile": profile,
            "base_url": _text(table.get("base_url")),
            "api_key": _text(table.get("api_key")),
            "env_key": _text(table.get("env_key")),
            "auth_mode": "bearer",
            "api_format": metadata.get("api_format")
            or _grok_api_format(table.get("api_backend")),
            "model": _text(table.get("model") or profile),
            "context_window": table.get("context_window"),
        }
    elif app_id == "opencode":
        options = _dict(native.get("options"))
        models = _dict(native.get("models"))
        form = {
            "npm": _text(native.get("npm")),
            "base_url": _text(options.get("baseURL") or options.get("base_url")),
            "api_key": _text(options.get("apiKey") or options.get("api_key")),
            "auth_mode": "bearer",
            "api_format": metadata.get("api_format") or native.get("api") or "openai_chat",
            "model": _text(native.get("model")) or next(iter(models), ""),
            "models": copy.deepcopy(models),
            "headers": copy.deepcopy(_dict(options.get("headers"))),
            "options": copy.deepcopy(options),
        }
    elif app_id == "openclaw":
        models = native.get("models") if isinstance(native.get("models"), list) else []
        form = {
            "base_url": _text(native.get("baseUrl") or native.get("base_url")),
            "api_key": _text(native.get("apiKey") or native.get("api_key")),
            "auth_mode": "bearer",
            "api_format": metadata.get("api_format") or native.get("api") or "openai_chat",
            "model": _first_model(models),
            "models": copy.deepcopy(models),
            "headers": copy.deepcopy(_dict(native.get("headers"))),
        }
    else:
        legacy_native = native.get("native_provider")
        if isinstance(legacy_native, dict):
            native = copy.deepcopy(legacy_native)
        models = native.get("models")
        form = {
            "base_url": _text(native.get("base_url") or native.get("baseUrl")),
            "api_key": _text(native.get("api_key") or native.get("apiKey")),
            "env_key": _text(native.get("env_key")),
            "auth_mode": _text(native.get("auth_mode")) or "bearer",
            "api_format": metadata.get("api_format")
            or native.get("api")
            or native.get("api_mode")
            or "openai_chat",
            "model": _text(native.get("model")) or _first_model(models),
            "models": copy.deepcopy(models if isinstance(models, (dict, list)) else {}),
            "headers": copy.deepcopy(_dict(native.get("headers"))),
        }

    legacy_routing = _dict(native.get("routing"))
    for key in (
        "base_url",
        "api_key",
        "auth_mode",
        "api_format",
        "model",
        "headers",
        "model_map",
    ):
        if legacy_routing.get(key) not in (None, ""):
            form[key] = copy.deepcopy(legacy_routing[key])
    if metadata.get("auth_mode") not in (None, ""):
        form["auth_mode"] = _text(metadata.get("auth_mode"))
    form["api_format"] = _normalize_api_format(form.get("api_format"), app_id)
    form["full_url"] = bool(metadata.get("full_url", metadata.get("isFullUrl", False)))
    form["use_outbound_proxy"] = bool(metadata.get("use_outbound_proxy", True))
    form["model_map"] = copy.deepcopy(_dict(metadata.get("model_map")))
    return form


def build_native_settings(
    app_id: str,
    form: Mapping[str, Any],
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _validate_app(app_id)
    values = dict(form)
    current = copy.deepcopy(dict(existing or {}))

    if app_id == "claude":
        result = _claude_settings(current)
        env = copy.deepcopy(_dict(result.get("env")))
        _put(env, "ANTHROPIC_BASE_URL", values.get("base_url"))
        _put(env, "ANTHROPIC_MODEL", values.get("model"))
        _put(env, "ANTHROPIC_DEFAULT_HAIKU_MODEL", values.get("haiku_model"))
        _put(env, "ANTHROPIC_DEFAULT_SONNET_MODEL", values.get("sonnet_model"))
        _put(env, "ANTHROPIC_DEFAULT_OPUS_MODEL", values.get("opus_model"))
        key_field = (
            "ANTHROPIC_AUTH_TOKEN"
            if _text(values.get("auth_mode")) == "bearer"
            else "ANTHROPIC_API_KEY"
        )
        _put(env, key_field, values.get("api_key"))
        result["env"] = env
        return result

    if app_id == "codex":
        result = current
        config_text = _text(result.get("config"))
        provider_key = _safe_provider_key(
            values.get("provider_key") or values.get("provider_id") or "default"
        )
        config_text = _toml_set_top_level(
            config_text,
            {
                "model_provider": provider_key,
                "model": _text(values.get("model")),
            },
        )
        block = {
            "name": _text(values.get("name")) or provider_key,
            "base_url": _text(values.get("base_url")),
            "wire_api": "responses"
            if _normalize_api_format(values.get("api_format"), app_id)
            == "openai_responses"
            else "chat",
            "requires_openai_auth": _text(values.get("auth_mode")) != "none",
        }
        config_text = _toml_replace_table(
            config_text, ("model_providers", provider_key), block
        )
        result["config"] = config_text
        auth = copy.deepcopy(_dict(result.get("auth")))
        _put(auth, "OPENAI_API_KEY", values.get("api_key"))
        result["auth"] = auth
        return result

    if app_id == "gemini":
        result = current
        env = _gemini_env(result.get("env"))
        _put(env, "GOOGLE_GEMINI_BASE_URL", values.get("base_url"))
        _put(env, "GEMINI_API_KEY", values.get("api_key"))
        _put(env, "GEMINI_MODEL", values.get("model"))
        result["env"] = env
        if "config" not in result and isinstance(result.get("settings"), dict):
            result["config"] = copy.deepcopy(result.pop("settings"))
        result.setdefault("config", {})
        return result

    if app_id == "grokbuild":
        result = current
        config_text = _text(result.get("config"))
        profile = _safe_provider_key(
            values.get("profile") or values.get("provider_id") or "default"
        )
        config_text = _toml_replace_table(config_text, ("models",), {"default": profile})
        table: dict[str, Any] = {
            "model": _text(values.get("model")) or profile,
            "base_url": _text(values.get("base_url")),
            "name": _text(values.get("name")) or profile,
            "api_backend": "responses"
            if _normalize_api_format(values.get("api_format"), app_id)
            == "openai_responses"
            else "chat_completions",
        }
        if values.get("context_window") not in (None, ""):
            table["context_window"] = int(values["context_window"])
        if _text(values.get("api_key")):
            table["api_key"] = _text(values.get("api_key"))
        if _text(values.get("env_key")):
            table["env_key"] = _text(values.get("env_key"))
        config_text = _toml_replace_table(config_text, ("model", profile), table)
        result["config"] = config_text
        return result

    if app_id == "opencode":
        result = current
        _put(result, "npm", values.get("npm"))
        options = copy.deepcopy(_dict(result.get("options")))
        _put(options, "baseURL", values.get("base_url"))
        _put(options, "apiKey", values.get("api_key"))
        if isinstance(values.get("headers"), dict):
            options["headers"] = copy.deepcopy(values["headers"])
        if isinstance(values.get("options"), dict):
            options = _deep_merge(options, values["options"])
        result["options"] = options
        if isinstance(values.get("models"), dict):
            result["models"] = copy.deepcopy(values["models"])
        _put(result, "model", values.get("model"))
        return result

    if app_id == "openclaw":
        result = current
        _put(result, "baseUrl", values.get("base_url"))
        _put(result, "apiKey", values.get("api_key"))
        _put(result, "api", _native_openclaw_api(values.get("api_format")))
        if isinstance(values.get("models"), list):
            result["models"] = copy.deepcopy(values["models"])
        if isinstance(values.get("headers"), dict):
            result["headers"] = copy.deepcopy(values["headers"])
        return result

    result = current
    _put(result, "base_url", values.get("base_url"))
    _put(result, "api_key", values.get("api_key"))
    _put(result, "env_key", values.get("env_key"))
    _put(result, "api", _native_hermes_api(values.get("api_format")))
    _put(result, "model", values.get("model"))
    if isinstance(values.get("models"), (dict, list)):
        result["models"] = copy.deepcopy(values["models"])
    if isinstance(values.get("headers"), dict):
        result["headers"] = copy.deepcopy(values["headers"])
    return result


def build_runtime_profile(
    app_id: str,
    settings: Mapping[str, Any],
    meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(meta or {})
    form = build_provider_form(app_id, settings, metadata)
    base_url = _text(form.get("base_url"))
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderAdapterError("provider base_url must use http or https")
    api_format = _normalize_api_format(form.get("api_format"), app_id)
    auth_mode = _text(form.get("auth_mode")) or _default_auth_mode(app_id)
    native_headers = _dict(form.get("headers"))
    meta_headers = _dict(metadata.get("headers"))
    return {
        "base_url": base_url if metadata.get("full_url") else base_url.rstrip("/"),
        "api_format": api_format,
        "auth_mode": auth_mode,
        "api_key": _text(form.get("api_key")) or None,
        "headers": {**native_headers, **meta_headers},
        "model": _text(form.get("model")) or None,
        "model_map": copy.deepcopy(_dict(metadata.get("model_map"))),
        "full_url": bool(metadata.get("full_url", False)),
        "use_outbound_proxy": bool(metadata.get("use_outbound_proxy", True)),
        "auto_failover": bool(metadata.get("auto_failover", False)),
        "max_retries": _bounded_int(metadata.get("max_retries", 0), 0, 10),
        "failure_threshold": _bounded_int(
            metadata.get("failure_threshold", 3), 1, 100
        ),
        "cooldown_seconds": _bounded_int(
            metadata.get("cooldown_seconds", 60), 0, 86400
        ),
    }


def read_provider_records(
    home: Path | str,
    app_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> list[dict[str, Any]]:
    _validate_app(app_id)
    home_path = Path(home)
    paths = resolve_agent_paths(
        app_id, home_path, environ=environ, overrides=overrides
    )
    records: list[dict[str, Any]] = []

    if app_id == "claude":
        path = paths.root / "settings.json"
        settings = _read_json(path)
        if settings is None:
            return []
        records.append(_record(app_id, "default", "当前配置", settings, {}))
    elif app_id == "codex":
        config_path = paths.root / "config.toml"
        auth_path = paths.root / "auth.json"
        if not config_path.exists() and not auth_path.exists():
            return []
        settings = {
            "config": _strip_toml_resource_tables(
                _read_text(config_path), {"mcp_servers"}
            ),
            "auth": _read_json(auth_path) or {},
        }
        records.append(_record(app_id, "default", "当前配置", settings, {}))
    elif app_id == "gemini":
        config = _read_json(paths.root / "settings.json") or {}
        config.pop("mcpServers", None)
        env = _gemini_env(_read_text(paths.root / ".env"))
        if not config and not env:
            return []
        records.append(
            _record(app_id, "default", "当前配置", {"env": env, "config": config}, {})
        )
    elif app_id == "grokbuild":
        path = paths.root / "config.toml"
        if not path.exists():
            return []
        settings = {
            "config": _strip_toml_resource_tables(
                _read_text(path), {"mcp_servers", "mcp"}
            )
        }
        parsed = _toml_load(settings["config"])
        official = not isinstance(parsed.get("models"), dict) and not isinstance(
            parsed.get("model"), dict
        )
        provider_id = "grokbuild-official" if official else "default"
        meta = {"official_mode": True} if official else {}
        records.append(_record(app_id, provider_id, "Grok Official" if official else "当前配置", settings, meta))
    elif app_id == "opencode":
        config = _read_json(paths.root / "opencode.json")
        providers = _dict(config.get("provider")) if config else {}
        current = _provider_prefix(config.get("model")) if config else ""
        for provider_id, settings in providers.items():
            if isinstance(settings, dict):
                records.append(
                    _record(app_id, str(provider_id), str(provider_id), settings, {}, is_current=current == str(provider_id))
                )
    elif app_id == "openclaw":
        config = _read_json(paths.root / "openclaw.json")
        if not config:
            return []
        providers = _dict(_dict(config.get("models")).get("providers"))
        primary = _dict(_dict(_dict(config.get("agents")).get("defaults")).get("model")).get("primary")
        current = _provider_prefix(primary)
        for provider_id, settings in providers.items():
            if isinstance(settings, dict):
                records.append(
                    _record(app_id, str(provider_id), str(provider_id), settings, {}, is_current=current == str(provider_id))
                )
    else:
        path = paths.root / "config.yaml"
        if not path.exists():
            return []
        try:
            config = yaml.safe_load(_read_text(path)) or {}
        except yaml.YAMLError as exc:
            raise ProviderAdapterError(f"Hermes config YAML 解析失败: {exc}") from exc
        if not isinstance(config, dict):
            raise ProviderAdapterError("Hermes config 必须是对象")
        current = _text(_dict(config.get("model")).get("provider"))
        seen: set[str] = set()
        custom = config.get("custom_providers")
        if isinstance(custom, list):
            for item in custom:
                if not isinstance(item, dict):
                    continue
                provider_id = _text(item.get("name"))
                if not provider_id or provider_id in seen:
                    continue
                seen.add(provider_id)
                records.append(
                    _record(
                        app_id,
                        provider_id,
                        provider_id,
                        item,
                        {"native_source": "custom_providers"},
                        is_current=current == provider_id,
                    )
                )
        for provider_key, item in _dict(config.get("providers")).items():
            if not isinstance(item, dict):
                continue
            provider_id = _text(item.get("name")) or str(provider_key)
            if provider_id in seen:
                continue
            seen.add(provider_id)
            settings = copy.deepcopy(item)
            settings.setdefault("name", provider_id)
            records.append(
                _record(
                    app_id,
                    provider_id,
                    provider_id,
                    settings,
                    {
                        "native_source": "providers_dict",
                        "native_read_only": True,
                        "read_only_reason": "Hermes 原生 providers 配置由客户端维护",
                    },
                    is_current=current in {provider_id, str(provider_key)},
                )
            )
    return records


def _record(
    app_id: str,
    provider_id: str,
    name: str,
    settings: Mapping[str, Any],
    meta: Mapping[str, Any],
    *,
    is_current: bool = True,
) -> dict[str, Any]:
    native = copy.deepcopy(dict(settings))
    metadata = {
        "native_mode": provider_mode(app_id),
        "import_kind": "default" if app_id in EXCLUSIVE_PROVIDER_APPS else "native_provider",
        **dict(meta),
    }
    return {
        "app_id": app_id,
        "provider_id": provider_id,
        "name": name,
        "settings": native,
        "meta": metadata,
        "is_current": bool(is_current),
        "source": "import",
        "form": build_provider_form(app_id, native, metadata),
        "summary": summarize_provider(app_id, native, metadata),
    }


def _validate_app(app_id: str) -> None:
    if app_id not in SUPPORTED_PROVIDER_APPS:
        raise ProviderAdapterError(f"unsupported provider client: {app_id}")


def _claude_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    wrapped = settings.get("config")
    if isinstance(wrapped, dict) and (
        settings.get("type") == "claude" or not settings.get("env")
    ):
        return copy.deepcopy(wrapped)
    return copy.deepcopy(dict(settings))


def _gemini_env(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    result: dict[str, str] = {}
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, item = line.split("=", 1)
        result[key.strip()] = item.strip().strip('"').strip("'")
    return result


def _toml_load(text: str) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(text or "")
    except tomllib.TOMLDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _toml_set_top_level(text: str, values: Mapping[str, Any]) -> str:
    lines = str(text or "").splitlines()
    first_table = next(
        (index for index, line in enumerate(lines) if line.strip().startswith("[")),
        len(lines),
    )
    prefix = lines[:first_table]
    suffix = lines[first_table:]
    for key, value in values.items():
        if value in (None, ""):
            continue
        rendered = f"{key} = {_toml_scalar(value)}"
        pattern = re.compile(r"^\s*" + re.escape(str(key)) + r"\s*=")
        match = next((index for index, line in enumerate(prefix) if pattern.match(line)), None)
        if match is None:
            prefix.append(rendered)
        else:
            prefix[match] = rendered
    rendered = "\n".join(prefix + suffix).rstrip() + "\n"
    tomllib.loads(rendered)
    return rendered


def _toml_replace_table(
    text: str, path: tuple[str, ...], values: Mapping[str, Any]
) -> str:
    header = "[" + ".".join(_toml_key(part) for part in path) + "]"
    pattern = re.compile(
        r"(?ms)^" + re.escape(header) + r"\s*\n.*?(?=^\[|\Z)"
    )
    block = header + "\n" + "\n".join(
        f"{_toml_key(str(key))} = {_toml_scalar(value)}"
        for key, value in values.items()
        if value not in (None, "")
    )
    source = pattern.sub("", str(text or "")).rstrip()
    rendered = (source + ("\n\n" if source else "") + block.rstrip() + "\n")
    tomllib.loads(rendered)
    return rendered


def _strip_toml_resource_tables(text: str, roots: set[str]) -> str:
    lines = str(text or "").splitlines(keepends=True)
    kept: list[str] = []
    dropping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            table = stripped.strip("[]").split(".", 1)[0].strip('"\'')
            dropping = table in roots
        if not dropping:
            kept.append(line)
    rendered = "".join(kept).rstrip() + ("\n" if kept else "")
    if rendered:
        tomllib.loads(rendered)
    return rendered


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _normalize_api_format(value: Any, app_id: str) -> str:
    normalized = _text(value).lower().replace("-", "_")
    return _API_FORMAT_ALIASES.get(normalized, _DEFAULT_API_FORMAT[app_id])


def _wire_api_format(value: Any, app_id: str) -> str:
    normalized = _text(value).lower()
    if normalized == "responses":
        return "openai_responses"
    if normalized in {"chat", "chat_completions"}:
        return "openai_chat"
    return _DEFAULT_API_FORMAT[app_id]


def _grok_api_format(value: Any) -> str:
    normalized = _text(value).lower().replace("-", "_")
    return "openai_chat" if normalized in {"chat", "chat_completions"} else "openai_responses"


def _native_openclaw_api(value: Any) -> str:
    return {
        "openai_responses": "openai-responses",
        "anthropic": "anthropic-messages",
        "gemini": "google-generative-ai",
    }.get(_normalize_api_format(value, "openclaw"), "openai-completions")


def _native_hermes_api(value: Any) -> str:
    return {
        "openai_responses": "openai-responses",
        "anthropic": "anthropic",
        "gemini": "gemini",
    }.get(_normalize_api_format(value, "hermes"), "openai-chat")


def _default_auth_mode(app_id: str) -> str:
    if app_id == "claude":
        return "x-api-key"
    if app_id == "gemini":
        return "query"
    return "bearer"


def _form_models(form: Mapping[str, Any]) -> list[str]:
    value = form.get("models")
    if isinstance(value, dict):
        return [str(key) for key in value]
    if isinstance(value, list):
        return [
            _text(item.get("id") or item.get("model") or item.get("name"))
            if isinstance(item, dict)
            else _text(item)
            for item in value
            if _text(item.get("id") or item.get("model") or item.get("name"))
            if isinstance(item, dict)
        ] if all(isinstance(item, dict) for item in value) else [_text(item) for item in value if _text(item)]
    return []


def _first_model(value: Any) -> str:
    if isinstance(value, dict):
        return next(iter(value), "")
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                candidate = _text(item.get("id") or item.get("model") or item.get("name"))
            else:
                candidate = _text(item)
            if candidate:
                return candidate
    return ""


def _has_credentials(value: Any, key_hint: str = "") -> bool:
    lowered = key_hint.lower()
    if isinstance(value, dict):
        return any(_has_credentials(child, str(key)) for key, child in value.items())
    if isinstance(value, list):
        return any(_has_credentials(child, key_hint) for child in value)
    return bool(_text(value)) and any(marker in lowered for marker in _SECRET_MARKERS)


def _provider_prefix(value: Any) -> str:
    text = _text(value)
    return text.split("/", 1)[0] if "/" in text else text


def _safe_provider_key(value: Any) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", _text(value)).strip("-_")
    candidate = candidate or "default"
    if candidate[0].isdigit():
        candidate = "provider_" + candidate
    return candidate.replace("-", "_")


def _put(target: dict[str, Any], key: str, value: Any) -> None:
    if value not in (None, ""):
        target[key] = copy.deepcopy(value)


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise ProviderAdapterError(f"Provider 配置 JSON 解析失败: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProviderAdapterError(f"Provider 配置必须是对象: {path}")
    return value


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ProviderAdapterError("router numeric setting must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ProviderAdapterError(
            f"router numeric setting must be between {minimum} and {maximum}"
        )
    return number
