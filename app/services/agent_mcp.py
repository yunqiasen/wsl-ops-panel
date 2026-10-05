from __future__ import annotations

import base64
import hashlib
import json
import tomllib

import yaml
from pathlib import Path
from typing import Any, Mapping

from app.services.agent_providers import redact_sensitive, restore_redacted
from app.services.state_store import PanelStateStore
from app.services.agent_mcp_adapters import project_mcp_spec, scan_mcp_home


MCP_STORE_FILENAME = "mcp_servers.json"
SUPPORTED_WRITE_APPS = {
    "codex",
    "claude",
    "gemini",
    "grokbuild",
    "opencode",
    "hermes",
}


def agent_data_root(config_root: Path | str) -> Path:
    root = Path(config_root)
    base = root.parent if root.name == "config" else root
    return base / "data" / "agent"


class AgentMcpStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / MCP_STORE_FILENAME
        self._state = PanelStateStore(self.data_root)
        self.state = self._state
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

    def observation_for(self, client_id: str, platform: str, mcp_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        def spec_hash(value):
            return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        expected = self.resolve_server_for_client(mcp_id, client_id, platform)
        observed_hash = spec_hash(spec)
        expected_hash = spec_hash(project_mcp_spec(client_id, dict(expected.get('spec') or {}))) if expected else None
        return {
            'mcp_id': mcp_id, 'present': True, 'spec_hash': observed_hash,
            'public_spec': redact_sensitive(spec),
            'status': 'drifted' if expected_hash is not None and expected_hash != observed_hash else 'installed',
        }

    def scan_home(self, home: Path, client_id: str, *, node_id: str = '__local__',
                  environ: Mapping[str, str] | None = None,
                  overrides: Mapping[str, Path | str] | None = None) -> dict[str, dict[str, Any]]:
        try:
            return scan_mcp_home(home, client_id, environ=environ, overrides=overrides)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            # Parser exceptions may quote secret-bearing source lines.
            error = f'{client_id} 配置读取失败 ({type(exc).__name__})'
            self.state.record_mcp_scan_error(node_id, client_id, error)
            raise ValueError(error) from None

    def refresh_observations(self, home: Path, client_id: str, *, node_id: str = '__local__', platform: str = 'linux') -> list[dict[str, Any]]:
        scanned = self.scan_home(home, client_id, node_id=node_id)
        observations = [self.observation_for(client_id, platform, key, spec) for key, spec in scanned.items()]
        self.state.replace_mcp_observations(node_id, client_id, observations)
        return observations

    def import_from_home(
        self,
        home: Path | str | None = None,
        apps: list[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Path | str] | None = None,
        node_id: str = "__local__",
        platform: str = "linux",
        server_ids: set[str] | None = None,
    ) -> int:
        home_path = Path(home) if home is not None else Path.home()
        selected_apps = list(
            dict.fromkeys(
                apps
                or (
                    "codex",
                    "claude",
                    "gemini",
                    "grokbuild",
                    "opencode",
                    "hermes",
                )
            )
        )
        imported_count = 0
        for client_id in selected_apps:
            if client_id not in SUPPORTED_WRITE_APPS:
                continue
            try:
                scanned = self.scan_home(home_path, client_id, node_id=node_id,
                                         environ=environ, overrides=overrides)
            except ValueError:
                continue
            partial_import = server_ids is not None
            selected_ids = {str(item) for item in (server_ids or set()) if str(item)}
            assignments_by_id = (
                {
                    str(item["mcp_id"]): dict(item)
                    for item in self._state.list_mcp_assignments(node_id, client_id)
                }
                if partial_import
                else {}
            )
            observations_by_id = (
                {
                    str(item["mcp_id"]): dict(item)
                    for item in self._state.list_mcp_observations(node_id, client_id)
                }
                if partial_import
                else {}
            )
            for mcp_id, spec in scanned.items():
                if partial_import and mcp_id not in selected_ids:
                    continue
                current = self.get_server(mcp_id)
                base_spec = dict(current.get("spec") or {}) if current else dict(spec)
                current_apps = dict(current.get("apps") or {}) if current else {}
                self._state.upsert_mcp_server(
                    mcp_id,
                    base_spec,
                    {**current_apps, client_id: True},
                    source=str(current.get("source") or "import") if current else "import",
                    name=str(current.get("name") or mcp_id) if current else mcp_id,
                    description=current.get("description") if current else None,
                    homepage=current.get("homepage") if current else None,
                    docs=current.get("docs") if current else None,
                    tags=list(current.get("tags") or []) if current else [],
                )
                self._state.upsert_mcp_variant(
                    mcp_id, client_id, platform, dict(spec), source="import"
                )
                assignments_by_id[mcp_id] = {
                    "mcp_id": mcp_id,
                    "variant_client_id": client_id,
                    "variant_platform": platform,
                }
                observations_by_id[mcp_id] = {
                    "mcp_id": mcp_id,
                    "present": True,
                    "spec_hash": _spec_hash(spec),
                    "public_spec": redact_sensitive(dict(spec)),
                    "status": "installed",
                }
                imported_count += 1
            self._state.replace_mcp_assignments(
                node_id, client_id, list(assignments_by_id.values())
            )
            self._state.replace_mcp_observations(
                node_id, client_id, list(observations_by_id.values())
            )
        return imported_count

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
        source: str = "manual",
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
            source=source,
            name=name or current.get("name") or server_id,
            description=description
            if description is not None
            else current.get("description"),
            homepage=homepage if homepage is not None else current.get("homepage"),
            docs=docs if docs is not None else current.get("docs"),
            tags=tags if tags is not None else current.get("tags", []),
        )

    def resolve_server_for_client(
        self, server_id: str, client_id: str, platform: str = "linux"
    ) -> dict[str, Any] | None:
        server = self.get_server(server_id)
        if server is None:
            return None
        variants = self._state.list_mcp_variants(server_id, client_id, platform)
        if not variants and platform != "any":
            variants = self._state.list_mcp_variants(server_id, client_id, "any")
        resolved = dict(server)
        if variants:
            resolved["spec"] = dict(variants[0].get("spec") or {})
            resolved["variant_client_id"] = client_id
            resolved["variant_platform"] = str(variants[0]["platform"])
        else:
            resolved["variant_client_id"] = "base"
            resolved["variant_platform"] = "any"
        return resolved

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


def _spec_hash(spec: Mapping[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def public_mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    safe = dict(server)
    safe["spec"] = redact_sensitive(dict(server.get("spec") or {}))
    return safe


def import_mcp_from_home(
    home: Path,
    apps: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Import the live MCP tables through the same client adapters used to apply them."""
    selected_apps = list(
        dict.fromkeys(
            apps
            or ("codex", "claude", "gemini", "grokbuild", "opencode", "hermes")
        )
    )
    imported: dict[str, dict[str, Any]] = {}
    for app_id in selected_apps:
        if app_id not in SUPPORTED_WRITE_APPS:
            # Claude Desktop/OpenClaw have no MCP projection in this workbench.
            continue
        try:
            scanned = scan_mcp_home(
                home, app_id, environ=environ, overrides=overrides
            )
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError, yaml.YAMLError):
            # Import is best-effort for malformed/absent client files; the
            # explicit scan endpoint reports parse errors separately.
            scanned = {}
        _merge_imported(imported, app_id, scanned)
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
            # 逻辑资源保持一个 ID；客户端差异由数据库 variant 保存。
            item.setdefault("apps", {})[app_id] = True
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
    path = home / '.codex' / 'config.toml'
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace") or "")
    table = data.get("mcp_servers", {})
    return table if isinstance(table, dict) else {}


def _read_grokbuild_mcp(home: Path) -> dict[str, dict[str, Any]]:
    path = home / '.grok' / 'config.toml'
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace") or "")
    table = data.get("mcp_servers", {})
    if not isinstance(table, dict):
        return {}
    return {
        str(server_id): _grokbuild_to_unified(spec)
        for server_id, spec in table.items()
        if isinstance(spec, dict)
    }


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


def _grokbuild_to_unified(spec: dict[str, Any]) -> dict[str, Any]:
    result = dict(spec)
    if "http_headers" in result and "headers" not in result:
        result["headers"] = result["http_headers"]
    result.pop("http_headers", None)
    transport = result.pop("type", None)
    if transport not in {"stdio", "http", "sse"}:
        transport = "http" if result.get("url") else "stdio"
    return {"type": transport, **result}


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



def _agent_script_client_path_helpers() -> str:
    """Shared path resolver embedded in local/remote MCP operation scripts."""
    return """
root_env = {
    'claude': 'CLAUDE_CONFIG_DIR',
    'codex': 'CODEX_HOME',
    'gemini': 'GEMINI_CLI_HOME',
    'grokbuild': 'GROK_HOME',
    'opencode': 'OPENCODE_CONFIG_DIR',
    'hermes': 'HERMES_HOME',
}
default_roots = {
    'claude': home / '.claude',
    'codex': home / '.codex',
    'gemini': home / '.gemini',
    'grokbuild': home / '.grok',
    'opencode': home / '.config' / 'opencode',
    'hermes': home / '.hermes',
}
default_rel = {
    'claude': pathlib.Path('.claude'),
    'codex': pathlib.Path('.codex'),
    'gemini': pathlib.Path('.gemini'),
    'grokbuild': pathlib.Path('.grok'),
    'opencode': pathlib.Path('.config') / 'opencode',
    'hermes': pathlib.Path('.hermes'),
}
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
    # Ignore only a login-shell default inherited by an isolated fixture Home.
    if home != system_home and path == system_home / default_rel[client]:
        return None
    return path

def client_root(client):
    return client_env_root(client) or default_roots[client]

def client_config(client, filename):
    override = client_env_root(client)
    if client == 'claude' and override is None:
        return home / '.claude.json'
    return (override or default_roots[client]) / filename
""".strip()

def _agent_mcp_apply_script(encoded_payload: str) -> str:
    return f"""
import base64, json, os, pathlib, pwd, shutil, tempfile, time, tomllib
payload = json.loads(base64.b64decode({encoded_payload!r}).decode('utf-8'))
home = pathlib.Path.home()
{_agent_script_client_path_helpers()}
servers = payload.get('servers', {{}})
apps = set(payload.get('apps', []))

def backup(path):
    path = pathlib.Path(path)
    if path.exists():
        bak = path.with_name(path.name + '.wsl-ops-agent-bak-' + time.strftime('%Y%m%d%H%M%S'))
        shutil.copy2(path, bak)
        print('backup', bak)
    path.parent.mkdir(parents=True, exist_ok=True)

def atomic_write(path, content):
    path = pathlib.Path(path)
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
        pathlib.Path(handle.name).unlink(missing_ok=True)

def q(value):
    return json.dumps(value, ensure_ascii=False)

def toml_key(value):
    raw = str(value)
    return raw if raw.replace('-', '').replace('_', '').isalnum() else q(raw)

def toml_scalar(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(toml_scalar(item) for item in value) + ']'
    if isinstance(value, dict):
        return '{{' + ', '.join(f'{{toml_key(k)}} = {{toml_scalar(v)}}' for k, v in value.items()) + '}}'
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

def codex_spec(spec):
    result = dict(spec) if isinstance(spec, dict) else {{}}
    if 'headers' in result:
        result['http_headers'] = result.pop('headers')
    result.setdefault('type', 'stdio')
    return result

def write_codex():
    path = client_config('codex', 'config.toml')
    old = path.read_text(encoding='utf-8', errors='replace') if path.exists() else ''
    try:
        old_payload = tomllib.loads(old or '')
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f'Codex config.toml parse failed: {{exc}}') from exc
    existing = old_payload.get('mcp_servers', {{}}) if isinstance(old_payload, dict) else {{}}
    selected = {{sid: codex_spec(item.get('spec') or {{}}) for sid, item in servers.items()}}
    merged = {{**existing, **selected}}
    body = [strip_codex_mcp(old).rstrip(), '']
    for sid, spec in merged.items():
        body.append(f'[mcp_servers.{{toml_key(sid)}}]')
        for key, value in spec.items():
            body.append(f'{{toml_key(key)}} = {{toml_scalar(value)}}')
        body.append('')
    rendered = '\\n'.join(body).strip() + '\\n'
    checked = tomllib.loads(rendered)
    table = checked.get('mcp_servers', {{}}) if isinstance(checked, dict) else {{}}
    if any(table.get(sid) != spec for sid, spec in selected.items()):
        raise SystemExit('Codex MCP render verification failed')
    backup(path)
    atomic_write(path, rendered)
    readback = tomllib.loads(path.read_text(encoding='utf-8')).get('mcp_servers', {{}})
    if any(readback.get(sid) != spec for sid, spec in selected.items()):
        raise SystemExit('Codex MCP readback verification failed')
    print('applied codex', path)

def grokbuild_spec(spec):
    result = {{}}
    for key, value in (spec.items() if isinstance(spec, dict) else []):
        if key == 'type':
            continue
        result['headers' if key == 'http_headers' else key] = value
    return result

def write_grokbuild():
    path = client_config('grokbuild', 'config.toml')
    old = path.read_text(encoding='utf-8', errors='replace') if path.exists() else ''
    try:
        old_payload = tomllib.loads(old or '')
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f'Grok Build config.toml parse failed: {{exc}}') from exc
    existing = old_payload.get('mcp_servers', {{}}) if isinstance(old_payload, dict) else {{}}
    selected = {{sid: grokbuild_spec(item.get('spec') or {{}}) for sid, item in servers.items()}}
    merged = {{**existing, **selected}}
    body = [strip_codex_mcp(old).rstrip(), '']
    for sid, spec in merged.items():
        body.append(f'[mcp_servers.{{toml_key(sid)}}]')
        for key, value in spec.items():
            body.append(f'{{toml_key(key)}} = {{toml_scalar(value)}}')
        body.append('')
    rendered = '\\n'.join(body).strip() + '\\n'
    checked = tomllib.loads(rendered)
    table = checked.get('mcp_servers', {{}}) if isinstance(checked, dict) else {{}}
    if not set(servers).issubset(table):
        raise SystemExit('Grok Build MCP render verification failed')
    backup(path)
    atomic_write(path, rendered)
    readback = tomllib.loads(path.read_text(encoding='utf-8')).get('mcp_servers', {{}})
    if not set(servers).issubset(readback):
        raise SystemExit('Grok Build MCP readback verification failed')
    print('applied grokbuild', path)

def read_json(path):
    if not path.exists():
        return {{}}
    try:
        data = json.loads(path.read_text(encoding='utf-8', errors='replace') or '{{}}')
    except json.JSONDecodeError as exc:
        raise SystemExit(f'JSON client config parse failed: {{path}}: {{exc}}') from exc
    if not isinstance(data, dict):
        raise SystemExit(f'JSON client config must be an object: {{path}}')
    return data

def write_json_mcp(path, key='mcpServers', transform=None):
    old = read_json(path)
    existing = old.get(key, {{}}) if isinstance(old.get(key), dict) else {{}}
    selected = {{sid: (transform(item.get('spec') or {{}}) if transform else item.get('spec') or {{}}) for sid, item in servers.items()}}
    old[key] = {{**existing, **selected}}
    rendered = json.dumps(old, ensure_ascii=False, indent=2) + '\\n'
    checked = json.loads(rendered)
    if not set(servers).issubset(checked.get(key, {{}})):
        raise SystemExit('JSON MCP render verification failed')
    backup(path)
    atomic_write(path, rendered)
    readback = read_json(path).get(key, {{}})
    if not set(servers).issubset(readback):
        raise SystemExit('JSON MCP readback verification failed')
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

def merge_hermes_spec(current, spec):
    core = ('type', 'command', 'args', 'env', 'url', 'headers', 'http_headers')
    result = {{str(key): value for key, value in current.items() if key not in core}}
    converted = hermes_spec(spec)
    for key, value in converted.items():
        if key == 'enabled' and key in result:
            continue
        result[key] = value
    return result

def write_hermes():
    path = client_config('hermes', 'config.yaml')
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit('Hermes MCP 写入需要 PyYAML，已停止以避免覆盖原配置') from exc
    old = {{}}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding='utf-8', errors='replace') or '') or {{}}
        except yaml.YAMLError as exc:
            raise SystemExit(f'Hermes config.yaml 解析失败，未写入: {{exc}}') from exc
        if not isinstance(loaded, dict):
            raise SystemExit('Hermes config.yaml must be a mapping')
        old = loaded
    existing = old.get('mcp_servers', {{}}) if isinstance(old.get('mcp_servers'), dict) else {{}}
    selected = {{}}
    for sid, item in servers.items():
        current = existing.get(sid, {{}})
        current = current if isinstance(current, dict) else {{}}
        selected[sid] = merge_hermes_spec(current, item.get('spec') or {{}})
    old['mcp_servers'] = {{**existing, **selected}}
    text = yaml.safe_dump(old, allow_unicode=True, sort_keys=False)
    checked = yaml.safe_load(text) or {{}}
    table = checked.get('mcp_servers', {{}}) if isinstance(checked, dict) else {{}}
    if not set(servers).issubset(table):
        raise SystemExit('Hermes MCP render verification failed')
    backup(path)
    atomic_write(path, text)
    readback = yaml.safe_load(path.read_text(encoding='utf-8')) or {{}}
    table = readback.get('mcp_servers', {{}}) if isinstance(readback, dict) else {{}}
    if not set(servers).issubset(table):
        raise SystemExit('Hermes MCP readback verification failed')
    print('applied hermes', path)


if 'codex' in apps:
    write_codex()
if 'claude' in apps:
    write_json_mcp(client_config('claude', '.claude.json'))
if 'gemini' in apps:
    write_json_mcp(client_config('gemini', 'settings.json'))
if 'grokbuild' in apps:
    write_grokbuild()
if 'opencode' in apps:
    write_json_mcp(client_config('opencode', 'opencode.json'), key='mcp', transform=opencode_spec)
if 'hermes' in apps:
    write_hermes()
""".strip()


def _agent_mcp_remove_script(encoded_payload: str) -> str:
    return f"""
import base64, json, os, pathlib, pwd, shutil, tempfile, time, tomllib
payload = json.loads(base64.b64decode({encoded_payload!r}).decode('utf-8'))
home = pathlib.Path.home()
{_agent_script_client_path_helpers()}
server_ids = set(payload.get('server_ids', []))
apps = set(payload.get('apps', []))

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
    if app in ('codex', 'grokbuild'):
        remove_codex(client_config(app, 'config.toml'))
    elif app == 'hermes':
        remove_hermes(client_config('hermes', 'config.yaml'))
    else:
        remove_json(client_config(app, 'opencode.json' if app == 'opencode' else 'settings.json' if app == 'gemini' else '.claude.json'), app)
    print('removed', app, ','.join(sorted(server_ids)))
""".strip()


def build_mcp_scan_shell(apps: list[str]) -> str:
    selected = [app for app in apps if app in SUPPORTED_WRITE_APPS]
    encoded_apps = base64.b64encode(json.dumps(selected).encode()).decode()
    script = f"""
import base64, json, os, pathlib, pwd, tomllib
home = pathlib.Path.home()
{_agent_script_client_path_helpers()}
apps = json.loads(base64.b64decode({encoded_apps!r}).decode())
result = {{}}

def codex_unified_spec(spec):
    result = dict(spec) if isinstance(spec, dict) else {{}}
    if 'http_headers' in result and 'headers' not in result:
        result['headers'] = result.get('http_headers')
    result.pop('http_headers', None)
    result.setdefault('type', 'stdio')
    return result

def grokbuild_spec(spec):
    result = dict(spec) if isinstance(spec, dict) else {{}}
    if 'http_headers' in result and 'headers' not in result:
        result['headers'] = result.get('http_headers')
    result.pop('http_headers', None)
    transport = result.pop('type', None)
    if transport not in ('stdio', 'http', 'sse'):
        transport = 'http' if result.get('url') else 'stdio'
    return {{'type': transport, **result}}

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
        path = client_config('codex', 'config.toml')
        if path.exists():
            try:
                payload = tomllib.loads(path.read_text(encoding="utf-8", errors="replace") or "")
                raw = payload.get("mcp_servers", {{}}) if isinstance(payload, dict) else {{}}
                table = {{str(key): codex_unified_spec(value) for key, value in raw.items() if isinstance(value, dict)}}
            except Exception: table = {{}}
    elif app == "grokbuild":
        path = client_config('grokbuild', 'config.toml')
        if path.exists():
            try:
                payload = tomllib.loads(path.read_text(encoding="utf-8", errors="replace") or "")
                raw = payload.get("mcp_servers", {{}}) if isinstance(payload, dict) else {{}}
                table = {{str(key): grokbuild_spec(value) for key, value in raw.items() if isinstance(value, dict)}}
            except Exception: table = {{}}
    elif app == "claude": table = json_table(client_config('claude', '.claude.json'))
    elif app == "gemini": table = json_table(client_config('gemini', 'settings.json'))
    elif app == "opencode": table = json_table(client_config('opencode', 'opencode.json'))
    elif app == "hermes":
        path = client_config('hermes', 'config.yaml')
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
