from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from app.services.agent_clients import get_agent_client


class RouteTakeoverError(ValueError):
    pass


class AgentRouteTakeover:
    """Apply and restore one client's local Router endpoint transactionally."""

    def __init__(self, home: Path | str, state_root: Path | str) -> None:
        self.home = Path(home)
        self.state_root = Path(state_root)
        self.state_path = self.state_root / "route-takeover.json"

    def enable(self, client_id: str, route_url: str) -> dict[str, Any]:
        client = get_agent_client(client_id)
        if client is None or "route" not in client.write_support:
            raise RouteTakeoverError(f"unsupported route takeover client: {client_id}")
        route = str(route_url).strip().rstrip("/")
        if not route.startswith(("http://", "https://")):
            raise RouteTakeoverError("route URL must use http or https")
        path = self._path(client_id)
        before_exists = path.exists()
        before = path.read_text(encoding="utf-8", errors="replace") if before_exists else ""
        records = self._read_state()
        existing = records.get(client_id)
        if isinstance(existing, dict) and existing.get("path") == str(path):
            # Keep the first snapshot as the restore point across repeated edits.
            original = str(existing.get("original_content") or "")
            original_exists = bool(existing.get("original_exists", False))
        else:
            original = before
            original_exists = before_exists
        rendered, owned = self._render_enabled(client_id, before, route)
        self._validate(client_id, rendered)
        _atomic_write(path, rendered)
        after = path.read_text(encoding="utf-8", errors="replace")
        if route not in after:
            raise RouteTakeoverError("route readback verification failed")
        records[client_id] = {
            "client_id": client_id,
            "path": str(path),
            "route_url": route,
            "original_content": original,
            "original_exists": original_exists,
            "original_hash": _hash(original),
            "enabled_hash": _hash(after),
            "owned": owned,
        }
        self._write_state(records)
        return {
            "client_id": client_id,
            "path": str(path),
            "route_url": route,
            "verified": True,
            "external_change": False,
        }

    def disable(self, client_id: str) -> dict[str, Any]:
        records = self._read_state()
        record = records.get(client_id)
        if not isinstance(record, dict):
            raise RouteTakeoverError(f"route takeover is not enabled: {client_id}")
        path = Path(str(record.get("path") or self._path(client_id)))
        current_exists = path.exists()
        current = path.read_text(encoding="utf-8", errors="replace") if current_exists else ""
        external_change = _hash(current) != str(record.get("enabled_hash") or "")
        original = str(record.get("original_content") or "")
        if not external_change:
            if bool(record.get("original_exists", False)):
                self._validate(client_id, original)
                _atomic_write(path, original)
            elif path.exists():
                path.unlink()
        else:
            rendered = self._restore_owned(
                client_id,
                current,
                record.get("owned") if isinstance(record.get("owned"), dict) else {},
                original,
                str(record.get("route_url") or ""),
            )
            self._validate(client_id, rendered)
            _atomic_write(path, rendered)
        after = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        route = str(record.get("route_url") or "")
        if route and route in after:
            raise RouteTakeoverError("route restore verification failed")
        records.pop(client_id, None)
        self._write_state(records)
        return {
            "client_id": client_id,
            "path": str(path),
            "verified": True,
            "external_change": external_change,
        }

    def status(self) -> dict[str, dict[str, Any]]:
        records = self._read_state()
        result: dict[str, dict[str, Any]] = {}
        for client_id, record in records.items():
            if not isinstance(record, dict):
                continue
            path = Path(str(record.get("path") or ""))
            current = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
            result[client_id] = {
                "enabled": True,
                "route_url": record.get("route_url"),
                "path": str(path),
                "external_change": _hash(current) != str(record.get("enabled_hash") or ""),
            }
        return result

    def _path(self, client_id: str) -> Path:
        paths = {
            "codex": self.home / ".codex" / "config.toml",
            "claude": self.home / ".claude.json",
            "gemini": self.home / ".gemini" / ".env",
            "opencode": self.home / ".config" / "opencode" / "opencode.json",
            "openclaw": self.home / ".openclaw" / "openclaw.json",
        }
        try:
            return paths[client_id]
        except KeyError as exc:
            raise RouteTakeoverError(f"unsupported route takeover client: {client_id}") from exc

    def _render_enabled(
        self, client_id: str, before: str, route: str
    ) -> tuple[str, dict[str, Any]]:
        if client_id == "codex":
            return _enable_codex(before, route)
        if client_id == "claude":
            return _enable_json_env(before, "ANTHROPIC_BASE_URL", route)
        if client_id == "gemini":
            return _enable_dotenv(before, "GOOGLE_GEMINI_BASE_URL", route)
        if client_id == "opencode":
            return _enable_opencode(before, route)
        if client_id == "openclaw":
            return _enable_openclaw(before, route)
        raise RouteTakeoverError(f"unsupported route takeover client: {client_id}")

    def _restore_owned(
        self,
        client_id: str,
        current: str,
        owned: dict[str, Any],
        original: str,
        route: str,
    ) -> str:
        if client_id == "codex":
            return _restore_codex(current, owned, original, route)
        if client_id in {"claude", "opencode", "openclaw"}:
            return _restore_json_owned(current, owned)
        if client_id == "gemini":
            return _restore_dotenv(current, owned)
        raise RouteTakeoverError(f"unsupported route takeover client: {client_id}")

    def _validate(self, client_id: str, content: str) -> None:
        if client_id == "codex":
            try:
                tomllib.loads(content or "")
            except tomllib.TOMLDecodeError as exc:
                raise RouteTakeoverError("Codex TOML validation failed") from exc
        elif client_id in {"claude", "opencode", "openclaw"}:
            try:
                payload = json.loads(content or "{}")
            except json.JSONDecodeError as exc:
                raise RouteTakeoverError("JSON configuration validation failed") from exc
            if not isinstance(payload, dict):
                raise RouteTakeoverError("JSON configuration must be an object")

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            raise RouteTakeoverError("route takeover state is invalid") from exc
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.state_root, delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        try:
            temporary.chmod(0o600)
            os.replace(temporary, self.state_path)
            self.state_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)


def _enable_codex(before: str, route: str) -> tuple[str, dict[str, Any]]:
    model_match = re.search(r"(?m)^model_provider\s*=\s*([\"'])(.*?)\1\s*$", before)
    old_provider = model_match.group(2) if model_match else None
    had_model_provider = model_match is not None
    if model_match:
        rendered = before[: model_match.start()] + 'model_provider = "wsl_agent_router"' + before[model_match.end() :]
    else:
        rendered = 'model_provider = "wsl_agent_router"\n' + before
    rendered = _replace_toml_table(
        rendered,
        "wsl_agent_router",
        f'[model_providers.wsl_agent_router]\nbase_url = {json.dumps(route)}\nwire_api = "responses"\n',
    )
    return rendered.rstrip() + "\n", {
        "kind": "codex",
        "old_provider": old_provider,
        "had_model_provider": had_model_provider,
        "route_provider": "wsl_agent_router",
    }


def _restore_codex(
    current: str, owned: dict[str, Any], original: str, route: str
) -> str:
    rendered = current
    route_provider = str(owned.get("route_provider") or "wsl_agent_router")
    # Remove the provider block we own, including a block renamed by an
    # external edit when its base URL still points at our route.
    pattern = re.compile(r"(?ms)^\[model_providers\.([^\]]+)\]\s*\n.*?(?=^\[|\Z)")
    blocks: list[str] = []
    for match in pattern.finditer(rendered):
        block = match.group(0)
        name = match.group(1)
        if name == route_provider or (route and route in block):
            continue
        blocks.append(block)
    if pattern.search(rendered):
        # Keep top-level content and all non-owned blocks in their existing order.
        first = pattern.search(rendered)
        assert first is not None
        suffix_start = first.start()
        top = rendered[:suffix_start]
        rendered = top + "".join(blocks)
    old_provider = owned.get("old_provider")
    model_match = re.search(r"(?m)^model_provider\s*=\s*([\"'])(.*?)\1\s*$", rendered)
    if old_provider is not None:
        line = f'model_provider = "{old_provider}"'
        if model_match:
            rendered = rendered[: model_match.start()] + line + rendered[model_match.end() :]
        else:
            rendered = line + "\n" + rendered
    elif not bool(owned.get("had_model_provider")) and model_match:
        rendered = rendered[: model_match.start()] + rendered[model_match.end() :]
    return rendered.rstrip() + "\n"


def _replace_toml_table(text: str, name: str, replacement: str) -> str:
    pattern = re.compile(rf"(?ms)^\[model_providers\.{re.escape(name)}\]\s*\n.*?(?=^\[|\Z)")
    if pattern.search(text):
        return pattern.sub(replacement.rstrip() + "\n", text, count=1)
    return text.rstrip() + "\n\n" + replacement.rstrip() + "\n"


def _enable_json_env(before: str, key: str, route: str) -> tuple[str, dict[str, Any]]:
    payload = _json_payload(before)
    env = payload.get("env")
    env = dict(env) if isinstance(env, dict) else {}
    old_present = key in env
    old_value = env.get(key)
    env[key] = route
    payload["env"] = env
    return _render_json(payload), {
        "kind": "json_env",
        "path": ["env", key],
        "old_present": old_present,
        "old_value": old_value,
    }


def _enable_opencode(before: str, route: str) -> tuple[str, dict[str, Any]]:
    payload = _json_payload(before)
    options = payload.get("options")
    options = dict(options) if isinstance(options, dict) else {}
    key = "baseURL" if "baseURL" in options or "base_url" not in options else "base_url"
    old_present = key in options
    old_value = options.get(key)
    options[key] = route
    payload["options"] = options
    return _render_json(payload), {
        "kind": "json_path",
        "path": ["options", key],
        "old_present": old_present,
        "old_value": old_value,
    }


def _enable_openclaw(before: str, route: str) -> tuple[str, dict[str, Any]]:
    payload = _json_payload(before)
    key = "baseUrl"
    old_present = key in payload
    old_value = payload.get(key)
    payload[key] = route
    return _render_json(payload), {
        "kind": "json_path",
        "path": [key],
        "old_present": old_present,
        "old_value": old_value,
    }


def _enable_dotenv(before: str, key: str, route: str) -> tuple[str, dict[str, Any]]:
    lines = before.splitlines()
    found = False
    old_present = False
    old_value: str | None = None
    rendered: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            found = True
            old_present = True
            old_value = line.split("=", 1)[1]
            rendered.append(f"{key}={route}")
        else:
            rendered.append(line)
    if not found:
        rendered.append(f"{key}={route}")
    return "\n".join(rendered).rstrip() + "\n", {
        "kind": "dotenv",
        "key": key,
        "old_present": old_present,
        "old_value": old_value,
    }


def _restore_json_owned(current: str, owned: dict[str, Any]) -> str:
    payload = _json_payload(current)
    path = owned.get("path")
    if not isinstance(path, list) or not path:
        return current
    cursor: dict[str, Any] = payload
    for key in path[:-1]:
        value = cursor.get(key)
        if not isinstance(value, dict):
            value = {}
            cursor[key] = value
        cursor = value
    leaf = str(path[-1])
    if owned.get("old_present"):
        cursor[leaf] = copy.deepcopy(owned.get("old_value"))
    else:
        cursor.pop(leaf, None)
    return _render_json(payload)


def _restore_dotenv(current: str, owned: dict[str, Any]) -> str:
    key = str(owned.get("key") or "GOOGLE_GEMINI_BASE_URL")
    lines = [line for line in current.splitlines() if not line.strip().startswith(f"{key}=")]
    if owned.get("old_present"):
        lines.append(f"{key}={owned.get('old_value')}")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


def _json_payload(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RouteTakeoverError("JSON configuration is invalid") from exc
    if not isinstance(payload, dict):
        raise RouteTakeoverError("JSON configuration must be an object")
    return payload


def _render_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
