from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.services.agent_providers import REDACTED_SECRET

SUPPORTED_API_FORMATS = {
    "anthropic",
    "openai_chat",
    "openai_responses",
    "gemini",
}
SUPPORTED_AUTH_MODES = {"bearer", "x-api-key", "query", "none"}


class ProviderProfileError(ValueError):
    pass


def normalize_provider_profile(
    app_id: str, settings: dict[str, Any]
) -> dict[str, Any]:
    routing = settings.get("routing")
    if isinstance(routing, dict):
        source = dict(routing)
    elif any(key in settings for key in ("base_url", "api_format", "api_key")):
        source = dict(settings)
    else:
        source = _legacy_profile(app_id, settings)

    base_url = str(source.get("base_url") or "").strip()
    if not base_url:
        raise ProviderProfileError("provider base_url is required")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ProviderProfileError("provider base_url must use http or https")

    api_format = _normalize_api_format(source.get("api_format"), app_id)
    auth_mode = str(source.get("auth_mode") or "").strip().lower()
    if not auth_mode:
        auth_mode = _default_auth_mode(api_format)
    if auth_mode not in SUPPORTED_AUTH_MODES:
        raise ProviderProfileError(f"unsupported auth_mode: {auth_mode}")

    api_key = source.get("api_key")
    if api_key is not None:
        api_key = str(api_key)
    headers = source.get("headers")
    clean_headers = (
        {str(key): str(value) for key, value in headers.items()}
        if isinstance(headers, dict)
        else {}
    )
    model_map = source.get("model_map")
    clean_model_map = (
        {str(key): str(value) for key, value in model_map.items()}
        if isinstance(model_map, dict)
        else {}
    )

    result: dict[str, Any] = {
        "base_url": base_url.rstrip("/") if not source.get("full_url") else base_url,
        "api_format": api_format,
        "auth_mode": auth_mode,
        "api_key": api_key,
        "headers": clean_headers,
        "model": str(source.get("model") or "").strip() or None,
        "model_map": clean_model_map,
        "full_url": bool(source.get("full_url", False)),
        "use_outbound_proxy": bool(source.get("use_outbound_proxy", True)),
    }
    secret_ref = str(source.get("secret_ref") or "").strip()
    if secret_ref:
        result["secret_ref"] = secret_ref
    return result


def public_runtime_provider(profile: dict[str, Any]) -> dict[str, Any]:
    public = dict(profile)
    if public.get("api_key"):
        public["api_key"] = REDACTED_SECRET
    headers = public.get("headers")
    if isinstance(headers, dict):
        public["headers"] = {
            str(key): REDACTED_SECRET for key, value in headers.items() if value is not None
        }
    return public


def _normalize_api_format(value: Any, app_id: str) -> str:
    candidate = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "openai": "openai_chat",
        "chat": "openai_chat",
        "chat_completions": "openai_chat",
        "responses": "openai_responses",
        "anthropic_messages": "anthropic",
        "gemini_native": "gemini",
    }
    candidate = aliases.get(candidate, candidate)
    if not candidate:
        candidate = {
            "claude": "anthropic",
            "claude-desktop": "anthropic",
            "codex": "openai_responses",
            "grokbuild": "openai_responses",
            "gemini": "gemini",
        }.get(app_id, "openai_chat")
    if candidate not in SUPPORTED_API_FORMATS:
        raise ProviderProfileError(f"unsupported api_format: {candidate}")
    return candidate


def _default_auth_mode(api_format: str) -> str:
    if api_format == "anthropic":
        return "x-api-key"
    if api_format == "gemini":
        return "query"
    return "bearer"


def _legacy_profile(app_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    if app_id == "claude":
        config = settings.get("config")
        env = config.get("env") if isinstance(config, dict) else None
        if not isinstance(env, dict):
            return {}
        token = env.get("ANTHROPIC_AUTH_TOKEN")
        return {
            "base_url": env.get("ANTHROPIC_BASE_URL"),
            "api_format": "anthropic",
            "api_key": token or env.get("ANTHROPIC_API_KEY"),
            "auth_mode": "bearer" if token else "x-api-key",
            "model": env.get("ANTHROPIC_MODEL"),
        }
    if app_id == "codex":
        auth = settings.get("auth")
        return {
            "base_url": settings.get("base_url"),
            "api_format": "openai_responses",
            "api_key": auth.get("OPENAI_API_KEY") if isinstance(auth, dict) else None,
            "model": settings.get("model"),
        }
    if app_id == "gemini":
        env = _parse_env(settings.get("env"))
        return {
            "base_url": env.get("GOOGLE_GEMINI_BASE_URL"),
            "api_format": "gemini",
            "api_key": env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"),
            "model": env.get("GEMINI_MODEL"),
        }
    if app_id == "opencode":
        config = settings.get("config")
        options = config.get("options") if isinstance(config, dict) else None
        if not isinstance(options, dict):
            return {}
        return {
            "base_url": options.get("baseURL") or options.get("base_url"),
            "api_format": config.get("api") or "openai_chat",
            "api_key": options.get("apiKey") or options.get("api_key"),
            "model": config.get("model"),
        }
    if app_id == "openclaw":
        config = settings.get("config")
        if not isinstance(config, dict):
            return {}
        return {
            "base_url": config.get("baseUrl") or config.get("base_url"),
            "api_format": config.get("api") or "openai_chat",
            "api_key": config.get("apiKey") or config.get("api_key"),
            "model": config.get("model"),
        }
    if app_id == "hermes":
        return {
            "base_url": settings.get("base_url"),
            "api_format": settings.get("api_mode") or "openai_chat",
            "api_key": settings.get("api_key"),
            "model": settings.get("model"),
        }
    return {}


def _parse_env(value: Any) -> dict[str, str]:
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
