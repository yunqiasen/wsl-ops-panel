from __future__ import annotations

import json
import shutil
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any

import yaml

CLIENT_PATHS = {
    "codex": ".codex/config.toml",
    "claude": ".claude.json",
    "gemini": ".gemini/settings.json",
    "opencode": ".config/opencode/opencode.json",
    "openclaw": ".openclaw/openclaw.json",
    "hermes": ".hermes/config.yaml",
}


def scan_mcp_home(home: Path, client_id: str) -> dict[str, dict[str, Any]]:
    path = home / CLIENT_PATHS[client_id]
    if not path.exists():
        return {}
    if client_id == "codex":
        payload = tomllib.loads(
            path.read_text(encoding="utf-8", errors="replace") or ""
        )
        table = payload.get("mcp_servers", {})
    elif client_id == "hermes":
        payload = (
            yaml.safe_load(path.read_text(encoding="utf-8", errors="replace") or "")
            or {}
        )
        table = payload.get("mcp_servers", {}) if isinstance(payload, dict) else {}
    else:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8", errors="replace") or "{}"
            )
        except json.JSONDecodeError:
            return {}
        table = _json_mcp_table(payload)
    if not isinstance(table, dict):
        return {}
    if client_id == "opencode":
        return {
            key: _opencode_to_canonical(value)
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


def apply_mcp_to_home(
    home: Path, client_id: str, selected: dict[str, dict[str, Any]]
) -> Path:
    path = home / CLIENT_PATHS[client_id]
    existing = scan_mcp_home(home, client_id)
    merged = {**existing, **selected}
    if client_id == "codex":
        old = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        rendered = _render_codex(old, merged)
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
        payload["mcp_servers"] = {
            key: _canonical_to_hermes(value) for key, value in merged.items()
        }
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
    verified = scan_mcp_home(home, client_id)
    if not set(selected).issubset(verified):
        raise RuntimeError(f"{client_id} MCP readback verification failed")
    return path


def remove_mcp_from_home(home: Path, client_id: str, server_ids: set[str]) -> Path:
    path = home / CLIENT_PATHS[client_id]
    if not path.exists():
        return path
    existing = scan_mcp_home(home, client_id)
    kept = {key: value for key, value in existing.items() if key not in server_ids}
    if client_id == "codex":
        old = path.read_text(encoding="utf-8", errors="replace")
        rendered = _render_codex(old, kept)
        tomllib.loads(rendered)
    elif client_id == "hermes":
        payload = (
            yaml.safe_load(path.read_text(encoding="utf-8", errors="replace") or "")
            or {}
        )
        if not isinstance(payload, dict):
            raise ValueError("Hermes config must be a mapping")
        payload["mcp_servers"] = {
            key: _canonical_to_hermes(value) for key, value in kept.items()
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
    verified = scan_mcp_home(home, client_id)
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
