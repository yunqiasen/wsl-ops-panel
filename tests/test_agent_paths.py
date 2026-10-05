from __future__ import annotations

from pathlib import Path

from app.services.agent_clients import agent_clients_payload
from app.services.agent_mcp_adapters import apply_mcp_to_home, scan_mcp_home
from app.services.agent_paths import resolve_agent_paths


def test_resolver_honors_hermes_home_and_explicit_codex_override(tmp_path: Path) -> None:
    hermes_root = tmp_path / "profiles/hermes"
    codex_root = tmp_path / "profiles/codex"

    hermes = resolve_agent_paths(
        "hermes", tmp_path / "home", environ={"HERMES_HOME": str(hermes_root)}
    )
    codex = resolve_agent_paths(
        "codex", tmp_path / "home", environ={}, overrides={"codex": codex_root}
    )

    assert hermes.root == hermes_root
    assert hermes.mcp == hermes_root / "config.yaml"
    assert hermes.prompt == hermes_root / "AGENTS.md"
    assert hermes.skills == hermes_root / "skills"
    assert codex.mcp == codex_root / "config.toml"
    assert codex.prompt == codex_root / "AGENTS.md"
    assert codex.route == codex_root / "config.toml"


def test_client_detection_uses_resolved_override_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    hermes_root = tmp_path / "custom-hermes"
    hermes_root.mkdir(parents=True)
    (hermes_root / "config.yaml").write_text("model: keep\n", encoding="utf-8")

    payload = agent_clients_payload(
        home,
        which=lambda _: None,
        environ={"HERMES_HOME": str(hermes_root)},
    )
    hermes = next(item for item in payload if item["id"] == "hermes")

    assert hermes["detected"] is True
    assert hermes["detection_source"] == "config"
    assert hermes["mcp_path"] == str(hermes_root / "config.yaml")


def test_mcp_adapter_reads_and_writes_resolved_hermes_home(tmp_path: Path) -> None:
    custom_root = tmp_path / "custom-hermes"
    environment = {"HERMES_HOME": str(custom_root)}

    path = apply_mcp_to_home(
        tmp_path / "home",
        "hermes",
        {"demo": {"type": "stdio", "command": "npx", "args": ["demo"]}},
        environ=environment,
    )

    assert path == custom_root / "config.yaml"
    assert scan_mcp_home(
        tmp_path / "home", "hermes", environ=environment
    )["demo"]["command"] == "npx"


def test_skills_prompts_and_route_takeover_use_the_same_resolver(
    tmp_path: Path,
) -> None:
    from app.services.agent_prompts import AgentPromptFileManager
    from app.services.agent_route_takeover import AgentRouteTakeover
    from app.services.agent_skills import install_skill_to_home, scan_agent_skills

    hermes_root = tmp_path / "hermes-profile"
    source = tmp_path / "skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    environment = {"HERMES_HOME": str(hermes_root)}

    scanned_before = scan_agent_skills(
        tmp_path / "home", environ=environment
    )
    assert scanned_before["hermes"]["path"] == str(hermes_root / "skills")
    installed = install_skill_to_home(
        tmp_path / "home",
        "hermes",
        "demo",
        str(source),
        environ=environment,
    )
    assert installed["path"] == str(hermes_root / "skills/demo")

    prompt = AgentPromptFileManager(
        tmp_path / "home", tmp_path / "state", environ=environment
    )
    assert prompt.status("hermes")["target"] == str(hermes_root / "AGENTS.md")

    takeover = AgentRouteTakeover(
        tmp_path / "home", tmp_path / "route-state", environ=environment
    )
    # Hermes deliberately has no route takeover, but the resolver-backed path
    # contract must still reject it without touching the profile.
    try:
        takeover.enable("hermes", "http://127.0.0.1:7888/hermes")
    except ValueError as exc:
        assert "unsupported" in str(exc)
    else:
        raise AssertionError("Hermes route takeover should remain unsupported")


def test_provider_import_uses_custom_client_roots(tmp_path: Path) -> None:
    from app.services.agent_providers import import_providers_from_home

    custom_root = tmp_path / "custom-hermes"
    custom_root.mkdir(parents=True)
    (custom_root / "config.yaml").write_text(
        "custom_providers:\n  - name: relay\n    base_url: https://relay.example/v1\n",
        encoding="utf-8",
    )

    providers = import_providers_from_home(
        tmp_path / "home",
        apps=["hermes"],
        environ={"HERMES_HOME": str(custom_root)},
    )

    assert providers[0]["app_id"] == "hermes"
    assert providers[0]["settings"]["config_path"] == str(custom_root / "config.yaml")


def test_provider_apply_shell_honors_hermes_home(tmp_path: Path) -> None:
    import os
    import subprocess

    from app.services.agent_providers import build_provider_apply_shell

    custom_root = tmp_path / "custom-hermes"
    command = build_provider_apply_shell(
        {
            "id": "relay",
            "name": "Relay",
            "app_id": "hermes",
            "settings_config": {
                "routing": {
                    "base_url": "https://relay.example/v1",
                    "api_format": "openai_chat",
                    "model": "relay-model",
                }
            },
        },
        write_secrets=False,
    )
    assert command is not None

    result = subprocess.run(
        ["bash", "-lc", command],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "HERMES_HOME": str(custom_root),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (custom_root / "config.yaml").is_file()
    assert not (tmp_path / "home/.hermes/config.yaml").exists()


def test_custom_fixture_home_precedes_ambient_client_home_without_explicit_environ(
    tmp_path: Path, monkeypatch
) -> None:
    """传入隔离 Home 时，宿主的 CODEX_HOME 不得把读写导向真实配置。"""
    ambient = tmp_path / "ambient-codex"
    ambient.mkdir(parents=True)
    (ambient / "config.toml").write_text(
        '[mcp_servers.ambient]\ncommand = "ambient"\n', encoding="utf-8"
    )
    fixture_home = tmp_path / "fixture-home"
    (fixture_home / ".codex").mkdir(parents=True)
    (fixture_home / ".codex" / "config.toml").write_text(
        '[mcp_servers.fixture]\ncommand = "fixture"\n', encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(ambient))

    resolved = resolve_agent_paths("codex", fixture_home)

    assert resolved.root == fixture_home / ".codex"
    assert resolved.mcp == fixture_home / ".codex" / "config.toml"


def test_explicit_empty_environ_disables_ambient_client_home(tmp_path: Path, monkeypatch) -> None:
    ambient = tmp_path / "ambient-codex"
    ambient.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(ambient))

    resolved = resolve_agent_paths("codex", tmp_path / "fixture-home", environ={})

    assert resolved.root == tmp_path / "fixture-home" / ".codex"
