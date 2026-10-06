"""Provider projection must not install, replace, or remove independent MCPs."""

import json
import tomllib

import pytest

from app.services.agent_provider_adapters import read_provider_records
from tests.test_provider_projection_lifecycle import fixture


@pytest.mark.parametrize("client_id", ["codex", "gemini", "grokbuild"])
@pytest.mark.parametrize(
    "entrypoint,write_secrets",
    [("activate", True), ("activate", False), ("profile", False)],
)
@pytest.mark.parametrize("installed", [True, False])
@pytest.mark.parametrize("readback_ok", [True, False])
def test_projection_preserves_target_mcp_not_stale_snapshot(
    tmp_path, monkeypatch, client_id, entrypoint, installed, write_secrets, readback_ok
):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    home = path.parents[1]
    dirname, variable = {
        "codex": (".codex", "CODEX_HOME"),
        "gemini": (".gemini", "GEMINI_CLI_HOME"),
        "grokbuild": (".grok", "GROK_HOME"),
    }[client_id]
    root = home / dirname
    root.mkdir(exist_ok=True)
    monkeypatch.setenv(variable, str(root))
    resource_key = "mcpServers" if client_id == "gemini" else "mcp_servers"
    if client_id == "gemini":
        path = root / "settings.json"
        path.write_text('{"theme":"light"}')
        (root / ".env").write_text("GEMINI_MODEL=old\nGEMINI_API_KEY=target-key\n")
    else:
        path = root / "config.toml"
        text = 'model = "old"\n'
        if client_id == "grokbuild":
            text = (
                '[models]\ndefault="fixture"\n[model.fixture]\n'
                'model="old"\nbase_url="https://fixture.example/v1"\n'
            )
        path.write_text(text)
    record = read_provider_records(home, client_id)[0]
    settings = record["settings"]
    # Legacy/manual snapshots may still contain MCPs. They are not install intent.
    if client_id == "gemini":
        settings["env"]["GEMINI_MODEL"] = "new"
        settings["config"][resource_key] = {"stale": {"command": "stale"}}
        current = json.loads(path.read_text())
        if installed:
            current[resource_key] = {
                "fixture.tool": {
                    "command": "fixture",
                    "env": {"TOKEN": "keep-target-secret"},
                }
            }
        path.write_text(json.dumps(current))
        expected = current.get(resource_key)
    else:
        settings["config"] = settings["config"].replace('"old"', '"new"')
        settings["config"] += '\n[mcp_servers.stale]\ncommand="stale"\n'
        current = path.read_text()
        if installed:
            current += (
                '\n[mcp_servers."fixture.tool"]\ncommand="fixture"\n'
                'args=["one", "two"]\nenv={TOKEN="keep-target-secret"}\n'
            )
        if client_id == "grokbuild":
            settings["config"] += '\n[mcp.stale]\ncommand="stale"\n'
            if installed:
                current += '\n[mcp."other.tool"]\ncommand="other"\n'
        path.write_text(current)
        expected = tomllib.loads(current).get(resource_key)
    store.upsert_provider(
        app_id=client_id, provider_id="new", name="new", settings=settings
    )
    before = path.read_text()
    if not readback_ok:
        monkeypatch.setattr(
            "app.services.agent_provider_projection.provider_matches",
            lambda *a, **kw: False,
        )
    if entrypoint == "activate":
        response = client.post(
            f"/api/agent/providers/{client_id}/new/activate",
            json={"write_secrets": write_secrets},
        )
        assert response.status_code == 202, response.text
        task = tasks.list_all()[0]
        app.state.task_worker._execute_task(task)
        assert tasks.get(task.id).status == ("succeeded" if readback_ok else "failed")
    else:
        # Resource/Profile reconciliation intentionally defaults to no new secrets.
        response = client.post(
            "/api/agent/resources/reconcile",
            json={
                "node_id": "__local__",
                "client_id": client_id,
                "resource_type": "provider",
                "resource_ids": ["new"],
                "action": "install",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["verified"] is readback_ok, response.text
    if not readback_ok:
        assert path.read_text() == before
        assert not store.get_provider(client_id, "new")["is_current"]
        return
    actual = (
        json.loads(path.read_text())
        if client_id == "gemini"
        else tomllib.loads(path.read_text())
    )
    assert actual.get(resource_key) == expected
    assert "stale" not in path.read_text()
    if client_id == "grokbuild":
        assert actual.get("mcp") == (
            {"other.tool": {"command": "other"}} if installed else None
        )
    assert store.get_provider(client_id, "new")["is_current"]


@pytest.mark.parametrize("client_id", ["codex", "grokbuild"])
@pytest.mark.parametrize("write_secrets", [True, False])
def test_toml_merge_preserves_native_types_and_secret_policy(
    tmp_path, monkeypatch, client_id, write_secrets
):
    import subprocess
    from app.services.agent_providers import build_provider_apply_shell

    home = tmp_path / "home"
    root = home / (".codex" if client_id == "codex" else ".grok")
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME" if client_id == "codex" else "GROK_HOME", str(root))
    path = root / "config.toml"
    extensions = """
[extension]
"中文名称" = "保留值"
day = 2026-10-05
clock = 12:34:56
stamp = 2026-10-05T12:34:56+08:00
[[extension.items]]
name = "item"
[[extension.items.children]]
value = 2
"""
    original = (
        'model="old"\napi_key="target-key"\n'
        + extensions
        + """
[mcp_servers."fixture.tool"]
command = "fixture"
env = {TOKEN="keep-target"}
"""
    )
    path.write_text(original)
    incoming = 'model="new"\napi_key="incoming-key"\n' + extensions
    shell = build_provider_apply_shell(
        {"id": "new", "app_id": client_id, "settings_config": {"config": incoming}},
        write_secrets=write_secrets,
    )
    result = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    actual = tomllib.loads(path.read_text())
    assert actual["extension"] == tomllib.loads(original)["extension"]
    assert actual["mcp_servers"] == tomllib.loads(original)["mcp_servers"]
    assert actual["api_key"] == ("incoming-key" if write_secrets else "target-key")
    assert actual["model"] == "new"


@pytest.mark.parametrize("client_id", ["codex", "gemini", "grokbuild"])
def test_invalid_target_fails_without_overwriting_or_committing(
    tmp_path, monkeypatch, client_id
):
    app, client, store, tasks, path = fixture(tmp_path, monkeypatch)
    home = path.parents[1]
    root_name, variable = {
        "codex": (".codex", "CODEX_HOME"),
        "gemini": (".gemini", "GEMINI_CLI_HOME"),
        "grokbuild": (".grok", "GROK_HOME"),
    }[client_id]
    root = home / root_name
    root.mkdir(exist_ok=True)
    monkeypatch.setenv(variable, str(root))
    path = root / ("settings.json" if client_id == "gemini" else "config.toml")
    original = (
        '{"mcpServers": broken' if client_id == "gemini" else "[mcp_servers.broken"
    )
    path.write_text(original)
    settings = (
        {"config": {"theme": "light"}}
        if client_id == "gemini"
        else {"config": 'model="new"\n'}
    )
    store.upsert_provider(
        app_id=client_id, provider_id="new", name="new", settings=settings
    )
    response = client.post(f"/api/agent/providers/{client_id}/new/activate", json={})
    assert response.status_code == 202, response.text
    task = tasks.list_all()[0]
    app.state.task_worker._execute_task(task)
    assert tasks.get(task.id).status == "failed"
    assert not store.get_provider(client_id, "new")["is_current"]
    assert path.read_text() == original


def test_codex_no_secret_write_still_updates_auth_flags_and_env_references(
    tmp_path, monkeypatch
):
    import subprocess
    from app.services.agent_providers import build_provider_apply_shell

    root = tmp_path / ".codex"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(root))
    path = root / "config.toml"
    original = (
        'model_provider="relay"\n[model_providers.relay]\n'
        'requires_openai_auth=true\nbearer_token_env_var="OLD_TOKEN"\n'
        'api_key="target-key"\n'
    )
    path.write_text(original)
    incoming = (
        original.replace("true", "false")
        .replace("OLD_TOKEN", "NEW_TOKEN")
        .replace("target-key", "incoming-key")
    )
    shell = build_provider_apply_shell(
        {"id": "new", "app_id": "codex", "settings_config": {"config": incoming}},
        write_secrets=False,
    )
    result = subprocess.run(
        ["bash", "-c", shell], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    provider = tomllib.loads(path.read_text())["model_providers"]["relay"]
    assert provider["requires_openai_auth"] is False
    assert provider["bearer_token_env_var"] == "NEW_TOKEN"
    assert provider["api_key"] == "target-key"
