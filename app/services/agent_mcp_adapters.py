from __future__ import annotations

import json
import shutil
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, Mapping

from app.services.agent_paths import resolve_agent_paths

import yaml

CLIENT_PATHS = {
    "codex": ".codex/config.toml",
    "claude": ".claude.json",
    "gemini": ".gemini/settings.json",
    "grokbuild": ".grok/config.toml",
    "opencode": ".config/opencode/opencode.json",
    "hermes": ".hermes/config.yaml",
}


def scan_mcp_home(
    home: Path,
    client_id: str,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, dict[str, Any]]:
    path = resolve_agent_paths(
        client_id, home, environ=environ, overrides=overrides
    ).mcp
    if path is None:
        raise ValueError(f"{client_id} does not expose an MCP configuration")
    if not path.exists():
        return {}
    if client_id in {"codex", "grokbuild"}:
        payload = tomllib.loads(
            path.read_text(encoding="utf-8", errors="strict") or ""
        )
        table = payload.get("mcp_servers", {})
    elif client_id == "hermes":
        payload = (
            yaml.safe_load(path.read_text(encoding="utf-8", errors="strict") or "")
            or {}
        )
        if not isinstance(payload, dict):
            raise ValueError('MCP configuration root must be an object')
        table = payload.get("mcp_servers", {})
    else:
        payload = json.loads(path.read_text(encoding="utf-8") or "{}")
        if not isinstance(payload, dict):
            raise ValueError('MCP configuration root must be an object')
        for key in ('mcpServers', 'mcp_servers', 'mcp', 'servers'):
            if key in payload and not isinstance(payload[key], dict):
                raise ValueError('MCP configuration table must be an object')
        table = _json_mcp_table(payload)
    if not isinstance(table, dict) or any(not isinstance(value, dict) for value in table.values()):
        raise ValueError('MCP configuration entries must be objects')
    if client_id == "codex":
        return {
            str(key): _codex_to_canonical(value)
            for key, value in table.items()
            if isinstance(value, dict)
        }
    if client_id == "opencode":
        return {
            key: _opencode_to_canonical(value)
            for key, value in table.items()
            if isinstance(value, dict)
        }
    if client_id == "grokbuild":
        return {
            str(key): _grokbuild_to_canonical(value)
            for key, value in table.items()
            if isinstance(value, dict)
        }
    if client_id == "hermes":
        return {
            key: _hermes_to_canonical(value)
            for key, value in table.items()
            if isinstance(value, dict)
        }
    return {
        str(key): dict(value) for key, value in table.items() if isinstance(value, dict)
    }


def _scan_with_context(
    home: Path,
    client_id: str,
    environ: Mapping[str, str] | None,
    overrides: Mapping[str, Path | str] | None,
) -> dict[str, dict[str, Any]]:
    # Keep compatibility with callers/tests that replace the legacy two-arg
    # scanner while still forwarding explicit profile context when supplied.
    if environ is None and overrides is None:
        return scan_mcp_home(home, client_id)
    return scan_mcp_home(
        home, client_id, environ=environ, overrides=overrides
    )


def apply_mcp_to_home(
    home: Path,
    client_id: str,
    selected: dict[str, dict[str, Any]],
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> Path:
    path = resolve_agent_paths(
        client_id, home, environ=environ, overrides=overrides
    ).mcp
    if path is None:
        raise ValueError(f"{client_id} does not expose an MCP configuration")
    existing = _scan_with_context(home, client_id, environ, overrides)
    merged = {**existing, **selected}
    if client_id == "codex":
        old = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        rendered = _render_codex(
            old,
            {
                key: _canonical_to_codex(value)
                for key, value in merged.items()
            },
        )
        tomllib.loads(rendered)
    elif client_id == "grokbuild":
        old = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        rendered = _render_codex(
            old,
            {
                key: _canonical_to_grokbuild(value)
                for key, value in merged.items()
            },
        )
        tomllib.loads(rendered)
    elif client_id == "hermes":
        payload: dict[str, Any] = {}
        if path.exists():
            loaded = (
                yaml.safe_load(path.read_text(encoding="utf-8", errors="replace") or "")
                or {}
            )
            if not isinstance(loaded, dict):
                raise ValueError("Hermes config must be a mapping")
            payload = loaded
        native = payload.get("mcp_servers", {})
        native = dict(native) if isinstance(native, dict) else {}
        for key, value in selected.items():
            current = native.get(key)
            native[key] = _merge_hermes_native(
                current if isinstance(current, dict) else {}, value
            )
        payload["mcp_servers"] = native
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        yaml.safe_load(rendered)
    else:
        payload = _read_json_object(path)
        key = _json_mcp_key(payload, client_id)
        values = merged
        if client_id == "opencode":
            values = {
                name: _canonical_to_opencode(spec) for name, spec in merged.items()
            }
        payload[key] = values
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        json.loads(rendered)
    _backup_and_atomic_write(path, rendered)
    verified = _scan_with_context(home, client_id, environ, overrides)
    mismatched = sorted(
        server_id
        for server_id, spec in selected.items()
        if verified.get(server_id) != project_mcp_spec(client_id, spec)
    )
    if mismatched:
        raise RuntimeError(
            f"{client_id} MCP readback verification failed: {', '.join(mismatched)}"
        )
    return path


def remove_mcp_from_home(
    home: Path,
    client_id: str,
    server_ids: set[str],
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> Path:
    path = resolve_agent_paths(
        client_id, home, environ=environ, overrides=overrides
    ).mcp
    if path is None:
        raise ValueError(f"{client_id} does not expose an MCP configuration")
    if not path.exists():
        return path
    existing = _scan_with_context(home, client_id, environ, overrides)
    kept = {key: value for key, value in existing.items() if key not in server_ids}
    if client_id == "codex":
        old = path.read_text(encoding="utf-8", errors="replace")
        rendered = _render_codex(
            old,
            {
                key: _canonical_to_codex(value)
                for key, value in kept.items()
            },
        )
        tomllib.loads(rendered)
    elif client_id == "grokbuild":
        old = path.read_text(encoding="utf-8", errors="replace")
        rendered = _render_codex(
            old,
            {
                key: _canonical_to_grokbuild(value)
                for key, value in kept.items()
            },
        )
        tomllib.loads(rendered)
    elif client_id == "hermes":
        payload = (
            yaml.safe_load(path.read_text(encoding="utf-8", errors="replace") or "")
            or {}
        )
        if not isinstance(payload, dict):
            raise ValueError("Hermes config must be a mapping")
        native = payload.get("mcp_servers", {})
        native = dict(native) if isinstance(native, dict) else {}
        payload["mcp_servers"] = {
            key: value for key, value in native.items() if key not in server_ids
        }
        rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        validated = yaml.safe_load(rendered)
        if not isinstance(validated, dict):
            raise ValueError("Hermes config must render as a mapping")
    else:
        payload = _read_json_object(path)
        key = _json_mcp_key(payload, client_id)
        payload[key] = (
            {name: _canonical_to_opencode(spec) for name, spec in kept.items()}
            if client_id == "opencode"
            else kept
        )
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        json.loads(rendered)
    _backup_and_atomic_write(path, rendered)
    verified = _scan_with_context(home, client_id, environ, overrides)
    remaining = sorted(server_ids.intersection(verified))
    if remaining:
        raise RuntimeError(
            f"{client_id} MCP readback verification failed: {', '.join(remaining)}"
        )
    return path


def _json_mcp_table(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    for key in ("mcpServers", "mcp_servers", "mcp", "servers"):
        if isinstance(payload.get(key), dict):
            return payload[key]
    return {}


def _json_mcp_key(payload: dict[str, Any], client_id: str) -> str:
    if client_id == "opencode":
        return "mcp"
    for key in ("mcpServers", "mcp_servers", "mcp", "servers"):
        if isinstance(payload.get(key), dict):
            return key
    return "mcpServers"


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON config: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON config must be an object: {path}")
    return payload


def _render_codex(old: str, servers: dict[str, dict[str, Any]]) -> str:
    lines = old.splitlines()
    kept: list[str] = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[mcp_servers"):
            skip = True
            continue
        if skip and stripped.startswith("["):
            skip = False
        if not skip:
            kept.append(line)
    body = ["\n".join(kept).rstrip(), ""]
    for server_id, spec in servers.items():
        body.append(f"[mcp_servers.{_toml_key(server_id)}]")
        for key, value in spec.items():
            body.append(f"{key} = {_toml_value(value)}")
        body.append("")
    return "\n".join(body).strip() + "\n"


def _toml_key(value: str) -> str:
    return (
        value
        if value.replace("-", "").replace("_", "").isalnum()
        else json.dumps(value)
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{_toml_key(str(key))} = {_toml_value(item)}"
                for key, item in value.items()
            )
            + "}"
        )
    return json.dumps(str(value), ensure_ascii=False)


def _codex_to_canonical(spec: dict[str, Any]) -> dict[str, Any]:
    result = dict(spec)
    if "http_headers" in result and "headers" not in result:
        result["headers"] = result["http_headers"]
    result.pop("http_headers", None)
    result.setdefault("type", "stdio")
    return result


def _canonical_to_codex(spec: dict[str, Any]) -> dict[str, Any]:
    result = dict(spec)
    if "headers" in result:
        result["http_headers"] = result.pop("headers")
    result.setdefault("type", "stdio")
    return result


def project_mcp_spec(
    client_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
    if client_id == "codex":
        return _codex_to_canonical(_canonical_to_codex(spec))
    if client_id == "grokbuild":
        return _grokbuild_to_canonical(_canonical_to_grokbuild(spec))
    if client_id == "opencode":
        return _opencode_to_canonical(_canonical_to_opencode(spec))
    if client_id == "hermes":
        return _hermes_to_canonical(_canonical_to_hermes(spec))
    return dict(spec)


def _opencode_to_canonical(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("type") == "remote":
        return {key: spec[key] for key in ("url", "headers") if key in spec} | {
            "type": "http"
        }
    command = spec.get("command", [])
    if isinstance(command, list):
        result: dict[str, Any] = {
            "type": "stdio",
            "command": command[0] if command else "",
        }
        if len(command) > 1:
            result["args"] = command[1:]
    else:
        result = {"type": "stdio", "command": command}
    if isinstance(spec.get("environment"), dict):
        result["env"] = spec["environment"]
    return result


def _grokbuild_to_canonical(spec: dict[str, Any]) -> dict[str, Any]:
    result = dict(spec)
    if "http_headers" in result and "headers" not in result:
        result["headers"] = result["http_headers"]
    result.pop("http_headers", None)
    transport = result.pop("type", None)
    if transport not in {"stdio", "http", "sse"}:
        transport = "http" if result.get("url") else "stdio"
    return {"type": transport, **result}


def _canonical_to_grokbuild(spec: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in spec.items():
        if key == "type":
            continue
        output_key = "headers" if key == "http_headers" else key
        result[output_key] = value
    return result


def _canonical_to_opencode(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("type") in {"http", "sse"}:
        return {
            "type": "remote",
            "enabled": True,
            **{key: spec[key] for key in ("url", "headers") if key in spec},
        }
    command = [spec.get("command", ""), *(spec.get("args") or [])]
    result: dict[str, Any] = {"type": "local", "enabled": True, "command": command}
    if isinstance(spec.get("env"), dict) and spec["env"]:
        result["environment"] = spec["env"]
    return result


def _hermes_to_canonical(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("url"):
        return {
            "type": "http",
            **{key: spec[key] for key in ("url", "headers") if key in spec},
        }
    return {
        "type": "stdio",
        **{key: spec[key] for key in ("command", "args", "env") if key in spec},
    }


def _canonical_to_hermes(spec: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"enabled": True}
    for key in (
        ("url", "headers")
        if spec.get("type") in {"http", "sse"}
        else ("command", "args", "env")
    ):
        if key in spec and spec[key]:
            result[key] = spec[key]
    return result


def _merge_hermes_native(
    existing: dict[str, Any], canonical: dict[str, Any]
) -> dict[str, Any]:
    core_fields = {
        "type",
        "command",
        "args",
        "env",
        "url",
        "headers",
        "http_headers",
    }
    result = {
        str(key): value for key, value in existing.items() if key not in core_fields
    }
    converted = _canonical_to_hermes(canonical)
    for key, value in converted.items():
        if key == "enabled" and key in result:
            continue
        result[key] = value
    return result


def _backup_and_atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(
            path.name + ".wsl-ops-agent-bak-" + time.strftime("%Y%m%d%H%M%S")
        )
        shutil.copy2(path, backup)
        backup.chmod(0o600)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.chmod(0o600)
    temporary.replace(path)
    path.chmod(0o600)
