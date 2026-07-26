from __future__ import annotations

import base64
import json
import tomllib

import yaml
from pathlib import Path
from typing import Any

from app.services.agent_providers import redact_sensitive, restore_redacted
from app.services.state_store import PanelStateStore


MCP_STORE_FILENAME = "mcp_servers.json"
SUPPORTED_WRITE_APPS = {"codex", "claude", "gemini", "opencode", "openclaw", "hermes"}


def agent_data_root(config_root: Path | str) -> Path:
    root = Path(config_root)
    base = root.parent if root.name == "config" else root
    return base / "data" / "agent"


class AgentMcpStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / MCP_STORE_FILENAME
        self._state = PanelStateStore(self.data_root)
        self._migrate_legacy_json_once()

    def list_servers(self) -> dict[str, dict[str, Any]]:
        return self._state.list_mcp_servers()

    def save_servers(self, servers: dict[str, dict[str, Any]]) -> None:
        for server_id, server in servers.items():
            self._state.upsert_mcp_server(
                server_id,
                dict(server.get("spec") or {}),
                {
                    str(app): bool(enabled)
                    for app, enabled in dict(server.get("apps") or {}).items()
                },
                source=str(server.get("source") or "manual"),
                name=str(server.get("name") or server_id),
                description=server.get("description")
                if isinstance(server.get("description"), str)
                else None,
                homepage=server.get("homepage")
                if isinstance(server.get("homepage"), str)
                else None,
                docs=server.get("docs")
                if isinstance(server.get("docs"), str)
                else None,
                tags=[str(tag) for tag in server.get("tags", [])]
                if isinstance(server.get("tags"), list)
                else [],
            )

    def import_from_home(
        self, home: Path | str | None = None, apps: list[str] | None = None
    ) -> int:
        imported = import_mcp_from_home(
            Path(home) if home is not None else Path.home(), apps=apps
        )
        if not imported:
            return 0
        for server_id, server in imported.items():
            existing_apps = self.list_servers().get(server_id, {}).get("apps", {})
            apps = {**existing_apps, **dict(server.get("apps") or {})}
            self._state.upsert_mcp_server(
                server_id,
                dict(server.get("spec") or {}),
                {str(app): bool(enabled) for app, enabled in apps.items()},
                source=str(server.get("source") or "import"),
                name=str(server.get("name") or server_id),
            )
        return len(imported)

    def upsert_server(
        self,
        server_id: str,
        spec: dict[str, Any],
        apps: dict[str, bool] | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        homepage: str | None = None,
        docs: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        current = self.list_servers().get(server_id, {})
        target_apps = {**dict(current.get("apps") or {}), **(apps or {})}
        merged_spec = (
            restore_redacted(spec, dict(current.get("spec") or {})) if current else spec
        )
        return self._state.upsert_mcp_server(
            server_id,
            merged_spec,
            target_apps,
            source="manual",
            name=name or current.get("name") or server_id,
            description=description
            if description is not None
            else current.get("description"),
            homepage=homepage if homepage is not None else current.get("homepage"),
            docs=docs if docs is not None else current.get("docs"),
            tags=tags if tags is not None else current.get("tags", []),
        )

    def selected_servers(self, server_ids: list[str]) -> dict[str, dict[str, Any]]:
        servers = self.list_servers()
        return {
            server_id: servers[server_id]
            for server_id in server_ids
            if server_id in servers
        }

    def get_server(self, server_id: str) -> dict[str, Any] | None:
        return self.list_servers().get(server_id)

    def delete_server(self, server_id: str) -> bool:
        return self._state.delete_mcp_server(server_id)

    def _migrate_legacy_json_once(self) -> None:
        if not self.path.exists() or self._state.list_mcp_servers():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        servers = payload.get("servers", {}) if isinstance(payload, dict) else {}
        if isinstance(servers, dict):
            self.save_servers(servers)


def public_mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    safe = dict(server)
    safe["spec"] = redact_sensitive(dict(server.get("spec") or {}))
    return safe


def import_mcp_from_home(
    home: Path, apps: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    selected_apps = set(
        apps or {"codex", "claude", "gemini", "opencode", "openclaw", "hermes"}
    )
    imported: dict[str, dict[str, Any]] = {}
    if "codex" in selected_apps:
        _merge_imported(imported, "codex", _read_codex_mcp(home))
    if "claude" in selected_apps:
        _merge_imported(imported, "claude", _read_json_mcp(home / ".claude.json"))
    if "gemini" in selected_apps:
        _merge_imported(
            imported, "gemini", _read_json_mcp(home / ".gemini" / "settings.json")
        )
    if "opencode" in selected_apps:
        _merge_imported(
            imported,
            "opencode",
            _read_opencode_mcp(home / ".config" / "opencode" / "opencode.json"),
        )
    if "openclaw" in selected_apps:
        _merge_imported(
            imported, "openclaw", _read_json_mcp(home / ".openclaw" / "openclaw.json")
        )
    if "hermes" in selected_apps:
        _merge_imported(
            imported, "hermes", _read_hermes_mcp(home / ".hermes" / "config.yaml")
        )
    return imported


def build_mcp_apply_shell(
    servers: dict[str, dict[str, Any]], apps: list[str], *, windows: bool = False
) -> str | None:
    selected_apps = [app for app in apps if app in SUPPORTED_WRITE_APPS]
    if windows or not selected_apps or not servers:
        return None
    payload = {"servers": servers, "apps": selected_apps}
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script = _agent_mcp_apply_script(encoded)
    return "python3 - <<'PY'\n" + script + "\nPY"


def build_mcp_remove_shell(
    server_ids: set[str], apps: list[str], *, windows: bool = False
) -> str | None:
    selected_apps = [app for app in apps if app in SUPPORTED_WRITE_APPS]
    selected_ids = sorted({server_id for server_id in server_ids if server_id})
    if windows or not selected_apps or not selected_ids:
        return None
    payload = {"server_ids": selected_ids, "apps": selected_apps}
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    script = _agent_mcp_remove_script(encoded)
    return "python3 - <<'PY'\n" + script + "\nPY"


def _merge_imported(
    target: dict[str, dict[str, Any]], app_id: str, servers: dict[str, dict[str, Any]]
) -> None:
    for server_id, spec in servers.items():
        item = target.get(server_id)
        if item is not None and item.get("spec") != spec:
            # 同名 MCP 在不同客户端可能有不同命令、参数或环境变量，不能互相覆盖。
            scoped_id = f"{server_id}--{app_id}"
            target[scoped_id] = {
                "id": scoped_id,
                "name": f"{server_id} ({app_id})",
                "spec": spec,
                "apps": {app_id: True},
                "source": app_id,
            }
            continue
        if item is None:
            item = {
                "id": server_id,
                "name": server_id,
                "spec": spec,
                "apps": {},
                "source": app_id,
            }
            target[server_id] = item
        item.setdefault("apps", {})[app_id] = True


def _read_codex_mcp(home: Path) -> dict[str, dict[str, Any]]:
    path = home / ".codex" / "config.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace") or "")
    table = data.get("mcp_servers", {})
    return table if isinstance(table, dict) else {}


def _read_json_mcp(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return {}
    return _extract_mcp_table(payload)


def _read_yaml_mcp(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = (
            yaml.safe_load(path.read_text(encoding="utf-8", errors="replace") or "")
            or {}
        )
    except yaml.YAMLError:
        return {}
    return _extract_mcp_table(payload)


def _extract_mcp_table(payload: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    for key in ("mcpServers", "mcp_servers", "mcp", "servers"):
        table = payload.get(key)
        if isinstance(table, dict):
            return table
    return {}


def _read_opencode_mcp(path: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json_mcp(path)
    return {
        server_id: _opencode_to_unified(spec)
        for server_id, spec in raw.items()
        if isinstance(spec, dict)
    }


def _read_hermes_mcp(path: Path) -> dict[str, dict[str, Any]]:
    raw = _read_yaml_mcp(path)
    return {
        server_id: _hermes_to_unified(spec)
        for server_id, spec in raw.items()
        if isinstance(spec, dict)
    }


def _opencode_to_unified(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("type") == "remote":
        result: dict[str, Any] = {"type": "sse"}
        if spec.get("url"):
            result["url"] = spec.get("url")
        if isinstance(spec.get("headers"), dict):
            result["headers"] = spec.get("headers")
        return result
    command = spec.get("command")
    result = {"type": "stdio"}
    if isinstance(command, list) and command:
        result["command"] = command[0]
        if len(command) > 1:
            result["args"] = command[1:]
    elif isinstance(command, str):
        result["command"] = command
    if isinstance(spec.get("environment"), dict):
        result["env"] = spec.get("environment")
    return result


def _hermes_to_unified(spec: dict[str, Any]) -> dict[str, Any]:
    if spec.get("url"):
        result: dict[str, Any] = {"type": "sse", "url": spec.get("url")}
        if isinstance(spec.get("headers"), dict):
            result["headers"] = spec.get("headers")
        return result
    result = {"type": "stdio"}
    if spec.get("command"):
        result["command"] = spec.get("command")
    if isinstance(spec.get("args"), list):
        result["args"] = spec.get("args")
    if isinstance(spec.get("env"), dict):
        result["env"] = spec.get("env")
    return result


def _agent_mcp_apply_script(encoded_payload: str) -> str:
    return f"""
import base64, json, os, pathlib, shutil, time, tomllib
payload = json.loads(base64.b64decode({encoded_payload!r}).decode('utf-8'))
home = pathlib.Path.home()
servers = payload.get('servers', {{}})
apps = set(payload.get('apps', []))

def backup(path):
    path = pathlib.Path(path)
    if path.exists():
        bak = path.with_name(path.name + '.wsl-ops-agent-bak-' + time.strftime('%Y%m%d%H%M%S'))
        shutil.copy2(path, bak)
        print('backup', bak)
    path.parent.mkdir(parents=True, exist_ok=True)

def q(value):
    return json.dumps(value, ensure_ascii=False)

def toml_scalar(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_scalar(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{{' + ', '.join(f'{{k}} = {{toml_scalar(v)}}' for k, v in value.items()) + '}}'
    return q(str(value))

def strip_codex_mcp(text):
    lines = text.splitlines()
    kept = []
    skip = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('[mcp_servers'):
            skip = True
            continue
        if skip and stripped.startswith('['):
            skip = False
        if not skip:
            kept.append(line)
    return '\\n'.join(kept).rstrip() + ('\\n' if kept else '')

def write_codex():
    path = home / '.codex' / 'config.toml'
    old = path.read_text(encoding='utf-8', errors='replace') if path.exists() else ''
    backup(path)
    try:
        old_payload = tomllib.loads(old or '')
        existing = old_payload.get('mcp_servers', {{}}) if isinstance(old_payload, dict) else {{}}
    except Exception:
        existing = {{}}
    merged = {{**existing, **{{sid: item.get('spec') or {{}} for sid, item in servers.items()}}}}
    body = [strip_codex_mcp(old).rstrip(), '']
    for sid, spec in merged.items():
        body.append(f'[mcp_servers.{{sid}}]')
        for key, value in spec.items():
            body.append(f'{{key}} = {{toml_scalar(value)}}')
        body.append('')
    path.write_text('\\n'.join(body).strip() + '\\n', encoding='utf-8')
    print('applied codex', path)

def read_json(path):
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8', errors='replace') or '{{}}')
            return data if isinstance(data, dict) else {{}}
        except Exception:
            return {{}}
    return {{}}

def write_json_mcp(path, key='mcpServers', transform=None):
    old = read_json(path)
    backup(path)
    existing = old.get(key, {{}}) if isinstance(old.get(key), dict) else {{}}
    selected = {{sid: (transform(item.get('spec') or {{}}) if transform else item.get('spec') or {{}}) for sid, item in servers.items()}}
    old[key] = {{**existing, **selected}}
    path.write_text(json.dumps(old, ensure_ascii=False, indent=2) + '\\n', encoding='utf-8')
    print('applied json', path, key)

def opencode_spec(spec):
    typ = spec.get('type', 'stdio') if isinstance(spec, dict) else 'stdio'
    if typ in ('sse', 'http'):
        result = {{'type': 'remote'}}
        if spec.get('url'):
            result['url'] = spec.get('url')
        if isinstance(spec.get('headers'), dict) and spec.get('headers'):
            result['headers'] = spec.get('headers')
        result['enabled'] = True
        return result
    result = {{'type': 'local', 'enabled': True}}
    cmd = spec.get('command', '') if isinstance(spec, dict) else ''
    args = spec.get('args', []) if isinstance(spec, dict) else []
    result['command'] = [cmd] + (args if isinstance(args, list) else [])
    if isinstance(spec.get('env'), dict) and spec.get('env'):
        result['environment'] = spec.get('env')
    return result

def hermes_spec(spec):
    typ = spec.get('type', 'stdio') if isinstance(spec, dict) else 'stdio'
    result = {{'enabled': True}}
    if typ in ('sse', 'http'):
        if spec.get('url'):
            result['url'] = spec.get('url')
        if isinstance(spec.get('headers'), dict) and spec.get('headers'):
            result['headers'] = spec.get('headers')
        return result
    for key in ('command', 'args', 'env'):
        if key in spec and spec.get(key):
            result[key] = spec.get(key)
    return result

def write_openclaw():
    path = home / '.openclaw' / 'openclaw.json'
    old = read_json(path)
    key = next((candidate for candidate in ('mcpServers', 'mcp_servers', 'mcp', 'servers') if isinstance(old.get(candidate), dict)), 'mcpServers')
    write_json_mcp(path, key=key)

def write_hermes():
    path = home / '.hermes' / 'config.yaml'
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('Hermes MCP 写入需要 PyYAML，已停止以避免覆盖原配置') from exc
    old = {{}}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding='utf-8', errors='replace') or '') or {{}}
            if isinstance(loaded, dict):
                old = loaded
        except Exception as exc:
            raise SystemExit(f'Hermes config.yaml 解析失败，未写入: {{exc}}') from exc
    existing = old.get('mcp_servers', {{}}) if isinstance(old.get('mcp_servers'), dict) else {{}}
    selected = {{sid: hermes_spec(item.get('spec') or {{}}) for sid, item in servers.items()}}
    old['mcp_servers'] = {{**existing, **selected}}
    backup(path)
    text = yaml.safe_dump(old, allow_unicode=True, sort_keys=False)
    path.write_text(text, encoding='utf-8')
    print('applied hermes', path)


if 'codex' in apps:
    write_codex()
if 'claude' in apps:
    write_json_mcp(home / '.claude.json')
if 'gemini' in apps:
    write_json_mcp(home / '.gemini' / 'settings.json')
if 'opencode' in apps:
    write_json_mcp(home / '.config' / 'opencode' / 'opencode.json', key='mcp', transform=opencode_spec)
if 'openclaw' in apps:
    write_openclaw()
if 'hermes' in apps:
    write_hermes()
""".strip()


def _agent_mcp_remove_script(encoded_payload: str) -> str:
    return f"""
import base64, json, os, pathlib, shutil, tempfile, time, tomllib
payload = json.loads(base64.b64decode({encoded_payload!r}).decode('utf-8'))
home = pathlib.Path.home()
server_ids = set(payload.get('server_ids', []))
apps = set(payload.get('apps', []))

paths = {{
    'codex': home / '.codex' / 'config.toml',
    'claude': home / '.claude.json',
    'gemini': home / '.gemini' / 'settings.json',
    'opencode': home / '.config' / 'opencode' / 'opencode.json',
    'openclaw': home / '.openclaw' / 'openclaw.json',
    'hermes': home / '.hermes' / 'config.yaml',
}}

def backup(path):
    if not path.exists():
        return
    target = path.with_name(path.name + '.wsl-ops-agent-bak-' + time.strftime('%Y%m%d%H%M%S'))
    shutil.copy2(path, target)
    target.chmod(0o600)

def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = pathlib.Path(handle.name)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary = pathlib.Path(handle.name)
        if temporary.exists():
            temporary.unlink()

def q(value):
    return json.dumps(value, ensure_ascii=False)

def toml_key(value):
    return value if value.replace('-', '').replace('_', '').isalnum() else q(value)

def toml_scalar(value):
    if isinstance(value, bool): return 'true' if value else 'false'
    if isinstance(value, (int, float)): return str(value)
    if isinstance(value, list): return '[' + ', '.join(toml_scalar(item) for item in value) + ']'
    if isinstance(value, dict): return '{{' + ', '.join(f'{{toml_key(str(k))}} = {{toml_scalar(v)}}' for k, v in value.items()) + '}}'
    return q(str(value))

def strip_codex_mcp(text):
    kept, skip = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[mcp_servers'):
            skip = True
            continue
        if skip and stripped.startswith('['):
            skip = False
        if not skip:
            kept.append(line)
    return '\\n'.join(kept).rstrip()

def remove_codex(path):
    if not path.exists(): return
    old = path.read_text(encoding='utf-8', errors='replace')
    parsed = tomllib.loads(old or '')
    table = parsed.get('mcp_servers', {{}}) if isinstance(parsed, dict) else {{}}
    kept = {{str(key): value for key, value in table.items() if str(key) not in server_ids and isinstance(value, dict)}}
    body = [strip_codex_mcp(old), '']
    for server_id, spec in kept.items():
        body.append(f'[mcp_servers.{{toml_key(server_id)}}]')
        for key, value in spec.items():
            body.append(f'{{toml_key(str(key))}} = {{toml_scalar(value)}}')
        body.append('')
    rendered = '\\n'.join(body).strip() + '\\n'
    tomllib.loads(rendered)
    backup(path)
    atomic_write(path, rendered)
    verified = tomllib.loads(path.read_text(encoding='utf-8')).get('mcp_servers', {{}})
    remaining = server_ids.intersection(verified)
    if remaining: raise SystemExit('Codex MCP readback verification failed: ' + ', '.join(sorted(remaining)))

def read_json(path):
    payload = json.loads(path.read_text(encoding='utf-8', errors='replace') or '{{}}')
    if not isinstance(payload, dict): raise SystemExit('JSON config must be an object')
    return payload

def table_key(payload, app):
    if app == 'opencode': return 'mcp'
    for key in ('mcpServers', 'mcp_servers', 'mcp', 'servers'):
        if isinstance(payload.get(key), dict): return key
    return 'mcpServers'

def remove_json(path, app):
    if not path.exists(): return
    payload = read_json(path)
    key = table_key(payload, app)
    table = payload.get(key, {{}}) if isinstance(payload.get(key), dict) else {{}}
    payload[key] = {{name: value for name, value in table.items() if name not in server_ids}}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + '\\n'
    json.loads(rendered)
    backup(path)
    atomic_write(path, rendered)
    verified = read_json(path).get(key, {{}})
    remaining = server_ids.intersection(verified if isinstance(verified, dict) else {{}})
    if remaining: raise SystemExit(app + ' MCP readback verification failed: ' + ', '.join(sorted(remaining)))

def remove_hermes(path):
    if not path.exists(): return
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('Hermes MCP 卸载需要 PyYAML') from exc
    payload = yaml.safe_load(path.read_text(encoding='utf-8', errors='replace') or '') or {{}}
    if not isinstance(payload, dict): raise SystemExit('Hermes config must be a mapping')
    table = payload.get('mcp_servers', {{}}) if isinstance(payload.get('mcp_servers'), dict) else {{}}
    payload['mcp_servers'] = {{name: value for name, value in table.items() if name not in server_ids}}
    rendered = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    if not isinstance(yaml.safe_load(rendered), dict): raise SystemExit('Hermes config validation failed')
    backup(path)
    atomic_write(path, rendered)
    verified = yaml.safe_load(path.read_text(encoding='utf-8')) or {{}}
    table = verified.get('mcp_servers', {{}}) if isinstance(verified, dict) else {{}}
    remaining = server_ids.intersection(table if isinstance(table, dict) else {{}})
    if remaining: raise SystemExit('Hermes MCP readback verification failed: ' + ', '.join(sorted(remaining)))

for app in sorted(apps):
    path = paths[app]
    if app == 'codex': remove_codex(path)
    elif app == 'hermes': remove_hermes(path)
    else: remove_json(path, app)
    print('removed', app, ','.join(sorted(server_ids)))
""".strip()


def build_mcp_scan_shell(apps: list[str]) -> str:
    selected = [app for app in apps if app in SUPPORTED_WRITE_APPS]
    encoded_apps = base64.b64encode(json.dumps(selected).encode()).decode()
    script = f"""
import base64, json, pathlib, tomllib
home = pathlib.Path.home()
apps = json.loads(base64.b64decode({encoded_apps!r}).decode())
result = {{}}

def json_table(path):
    if not path.exists(): return {{}}
    try: payload = json.loads(path.read_text(encoding="utf-8", errors="replace") or "{{}}")
    except Exception: return {{}}
    if not isinstance(payload, dict): return {{}}
    for key in ("mcpServers", "mcp_servers", "mcp", "servers"):
        if isinstance(payload.get(key), dict): return payload[key]
    return {{}}

for app in apps:
    table = {{}}
    if app == "codex":
        path = home / ".codex" / "config.toml"
        if path.exists():
            try:
                payload = tomllib.loads(path.read_text(encoding="utf-8", errors="replace") or "")
                table = payload.get("mcp_servers", {{}}) if isinstance(payload, dict) else {{}}
            except Exception: table = {{}}
    elif app == "claude": table = json_table(home / ".claude.json")
    elif app == "gemini": table = json_table(home / ".gemini" / "settings.json")
    elif app == "opencode": table = json_table(home / ".config" / "opencode" / "opencode.json")
    elif app == "openclaw": table = json_table(home / ".openclaw" / "openclaw.json")
    elif app == "hermes":
        path = home / ".hermes" / "config.yaml"
        if path.exists():
            try:
                import yaml
                payload = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace") or "") or {{}}
                table = payload.get("mcp_servers", {{}}) if isinstance(payload, dict) else {{}}
            except Exception: table = {{}}
    result[app] = {{str(k): v for k, v in table.items() if isinstance(v, dict)}}
print("WSL_OPS_MCP_SCAN:" + base64.b64encode(json.dumps(result, ensure_ascii=False).encode()).decode())
""".strip()
    return "python3 - <<'PY'\n" + script + "\nPY"


def parse_mcp_scan_output(output: str) -> dict[str, dict[str, dict[str, Any]]]:
    marker = "WSL_OPS_MCP_SCAN:"
    line = next(
        (line for line in reversed(output.splitlines()) if line.startswith(marker)),
        None,
    )
    if line is None:
        raise ValueError("远程 MCP 扫描没有返回有效结果")
    payload = json.loads(base64.b64decode(line[len(marker) :]).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("远程 MCP 扫描结果格式无效")
    return {
        str(app): {
            str(mcp_id): dict(spec)
            for mcp_id, spec in servers.items()
            if isinstance(spec, dict)
        }
        for app, servers in payload.items()
        if isinstance(servers, dict)
    }
