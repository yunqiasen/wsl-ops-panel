from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from app.services.agent_provider_adapters import (
    ADDITIVE_PROVIDER_APPS,
    EXCLUSIVE_PROVIDER_APPS,
    build_native_settings,
    build_provider_form,
    build_runtime_profile,
    read_provider_records,
    summarize_provider,
)


@pytest.mark.parametrize(
    ("app_id", "settings", "meta", "expected"),
    [
        (
            "claude",
            {
                "$schema": "https://json.schemastore.org/claude-code-settings.json",
                "env": {
                    "ANTHROPIC_BASE_URL": "https://claude.example/v1",
                    "ANTHROPIC_AUTH_TOKEN": "claude-key",
                    "ANTHROPIC_MODEL": "claude-main",
                },
            },
            {"api_format": "anthropic"},
            ("https://claude.example/v1", "claude-main", "anthropic"),
        ),
        (
            "codex",
            {
                "auth": {"OPENAI_API_KEY": "codex-key"},
                "config": (
                    'model = "gpt-5.6"\nmodel_provider = "relay"\n'
                    '[model_providers.relay]\nbase_url = "https://codex.example/v1"\n'
                    'wire_api = "responses"\nrequires_openai_auth = true\n'
                ),
            },
            {},
            ("https://codex.example/v1", "gpt-5.6", "openai_responses"),
        ),
        (
            "gemini",
            {
                "env": {
                    "GOOGLE_GEMINI_BASE_URL": "https://gemini.example",
                    "GEMINI_API_KEY": "gemini-key",
                    "GEMINI_MODEL": "gemini-2.5-pro",
                },
                "config": {"ui": {"theme": "dark"}},
            },
            {},
            ("https://gemini.example", "gemini-2.5-pro", "gemini"),
        ),
        (
            "grokbuild",
            {
                "config": (
                    '[models]\ndefault = "relay"\n\n[model.relay]\n'
                    'model = "grok-4"\nbase_url = "https://grok.example/v1"\n'
                    'api_backend = "responses"\nenv_key = "XAI_API_KEY"\n'
                )
            },
            {},
            ("https://grok.example/v1", "grok-4", "openai_responses"),
        ),
        (
            "opencode",
            {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": "https://oc.example/v1", "apiKey": "oc-key"},
                "models": {"gpt-5": {"name": "GPT-5"}},
            },
            {"api_format": "openai_chat"},
            ("https://oc.example/v1", "gpt-5", "openai_chat"),
        ),
        (
            "openclaw",
            {
                "baseUrl": "https://claw.example/v1",
                "apiKey": "claw-key",
                "api": "openai-responses",
                "models": [{"id": "gpt-5.6", "name": "GPT-5.6"}],
            },
            {},
            ("https://claw.example/v1", "gpt-5.6", "openai_responses"),
        ),
        (
            "hermes",
            {
                "name": "relay",
                "base_url": "https://hermes.example/v1",
                "api_key": "hermes-key",
                "api": "openai-chat",
                "models": {"gpt-5": {"name": "GPT-5"}},
            },
            {},
            ("https://hermes.example/v1", "gpt-5", "openai_chat"),
        ),
    ],
)
def test_summarize_provider_uses_each_clients_native_shape(
    app_id: str,
    settings: dict[str, object],
    meta: dict[str, object],
    expected: tuple[str, str, str],
) -> None:
    summary = summarize_provider(app_id, settings, meta)

    assert (summary["base_url"], summary["model"], summary["api_format"]) == expected
    assert summary["mode"] == (
        "exclusive" if app_id in EXCLUSIVE_PROVIDER_APPS else "additive"
    )
    assert summary["has_credentials"] is True
    assert summary["editable"] is True


def test_build_native_settings_preserves_unknown_fields_for_all_clients() -> None:
    fixtures = {
        "claude": (
            {"env": {"KEEP": "yes"}, "hooks": {"keep": True}},
            {"base_url": "https://new.example", "api_key": "k", "model": "new"},
            lambda value: value["hooks"]["keep"] is True
            and value["env"]["KEEP"] == "yes",
        ),
        "codex": (
            {
                "auth": {"KEEP": "yes"},
                "config": 'model_provider = "old"\n[future]\nkeep = true\n',
            },
            {
                "provider_id": "relay",
                "provider_key": "relay",
                "name": "Relay",
                "base_url": "https://new.example/v1",
                "api_key": "k",
                "model": "gpt-5.6",
                "api_format": "openai_responses",
            },
            lambda value: tomllib.loads(value["config"])["future"]["keep"] is True
            and value["auth"]["KEEP"] == "yes",
        ),
        "gemini": (
            {"env": {"KEEP": "yes"}, "config": {"ui": {"theme": "dark"}}},
            {"base_url": "https://new.example", "api_key": "k", "model": "gemini"},
            lambda value: value["env"]["KEEP"] == "yes"
            and value["config"]["ui"]["theme"] == "dark",
        ),
        "grokbuild": (
            {"config": '[future]\nkeep = true\n'},
            {
                "provider_id": "relay",
                "profile": "relay",
                "name": "Relay",
                "base_url": "https://new.example/v1",
                "api_key": "k",
                "env_key": "XAI_API_KEY",
                "model": "grok-4",
                "api_format": "openai_responses",
            },
            lambda value: tomllib.loads(value["config"])["future"]["keep"] is True,
        ),
        "opencode": (
            {"npm": "@ai-sdk/openai-compatible", "future": {"keep": True}, "options": {}},
            {"base_url": "https://new.example", "api_key": "k", "models": {"m": {}}},
            lambda value: value["future"]["keep"] is True,
        ),
        "openclaw": (
            {"future": {"keep": True}, "models": []},
            {"base_url": "https://new.example", "api_key": "k", "models": [{"id": "m"}]},
            lambda value: value["future"]["keep"] is True,
        ),
        "hermes": (
            {"future": {"keep": True}, "models": {}},
            {"base_url": "https://new.example", "api_key": "k", "models": {"m": {}}},
            lambda value: value["future"]["keep"] is True,
        ),
    }

    for app_id, (existing, form, assertion) in fixtures.items():
        native = build_native_settings(app_id, form, existing)
        assert assertion(native), app_id
        assert build_provider_form(app_id, native, {})["base_url"] == form["base_url"]


def test_runtime_profile_separates_router_metadata_from_native_settings() -> None:
    settings = {
        "auth": {"OPENAI_API_KEY": "secret"},
        "config": (
            'model = "gpt-5.6"\nmodel_provider = "relay"\n'
            '[model_providers.relay]\nbase_url = "https://relay.example/v1"\n'
            'wire_api = "responses"\n'
        ),
    }
    profile = build_runtime_profile(
        "codex",
        settings,
        {
            "model_map": {"gpt-5.6": "upstream"},
            "full_url": True,
            "use_outbound_proxy": False,
            "headers": {"x-route": "one"},
        },
    )

    assert profile == {
        "base_url": "https://relay.example/v1",
        "api_format": "openai_responses",
        "auth_mode": "bearer",
        "api_key": "secret",
        "headers": {"x-route": "one"},
        "model": "gpt-5.6",
        "model_map": {"gpt-5.6": "upstream"},
        "full_url": True,
        "use_outbound_proxy": False,
        "auto_failover": False,
        "max_retries": 0,
        "failure_threshold": 3,
        "cooldown_seconds": 60,
    }
    assert "model_map" not in settings


def test_read_provider_records_uses_real_exclusive_provider_files(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://claude.example/v1",
                    "ANTHROPIC_AUTH_TOKEN": "claude-key",
                    "ANTHROPIC_MODEL": "claude-main",
                },
                "hooks": {"keep": True},
            }
        )
    )
    (tmp_path / ".claude.json").write_text(
        json.dumps({"mcpServers": {"not-a-provider": {"url": "https://wrong.example"}}})
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        'model = "gpt-5.6"\nmodel_provider = "relay"\n'
        '[model_providers.relay]\nbase_url = "https://codex.example/v1"\nwire_api = "responses"\n'
        '[mcp_servers.context7]\nurl = "https://mcp.example"\n'
    )
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "codex-key"})
    )
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text(
        json.dumps({"ui": {"theme": "dark"}, "mcpServers": {"drop": {"url": "x"}}})
    )
    (tmp_path / ".gemini" / ".env").write_text(
        "GOOGLE_GEMINI_BASE_URL=https://gemini.example\n"
        "GEMINI_API_KEY=gemini-key\nGEMINI_MODEL=gemini-2.5-pro\n"
    )
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "config.toml").write_text(
        '[models]\ndefault = "relay"\n[model.relay]\nmodel = "grok-4"\n'
        'base_url = "https://grok.example/v1"\napi_backend = "responses"\n'
    )

    by_app = {
        app_id: read_provider_records(tmp_path, app_id)
        for app_id in sorted(EXCLUSIVE_PROVIDER_APPS)
    }

    assert all([row[0]["provider_id"] == "default" for row in by_app.values()])
    assert by_app["claude"][0]["settings"]["hooks"] == {"keep": True}
    assert "mcpServers" not in by_app["gemini"][0]["settings"]["config"]
    assert "mcp_servers" not in tomllib.loads(by_app["codex"][0]["settings"]["config"])
    assert by_app["grokbuild"][0]["form"]["profile"] == "relay"


def test_read_provider_records_splits_additive_client_providers(tmp_path: Path) -> None:
    opencode = tmp_path / ".config" / "opencode"
    opencode.mkdir(parents=True)
    (opencode / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "one": {"npm": "@ai-sdk/openai", "options": {"baseURL": "https://one"}},
                    "two": {"npm": "@ai-sdk/openai", "options": {"baseURL": "https://two"}},
                },
                "mcp": {"drop": {"type": "remote", "url": "https://mcp"}},
            }
        )
    )
    (tmp_path / ".openclaw").mkdir()
    (tmp_path / ".openclaw" / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {
                    "mode": "merge",
                    "providers": {
                        "one": {"baseUrl": "https://one", "models": [{"id": "m1"}]},
                        "two": {"baseUrl": "https://two", "models": [{"id": "m2"}]},
                    },
                },
                "agents": {"defaults": {"model": {"primary": "two/m2"}}},
            }
        )
    )
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "config.yaml").write_text(
        "model:\n  provider: custom-one\n  default: m1\n"
        "custom_providers:\n"
        "  - name: custom-one\n    base_url: https://one\n    models: {m1: {}}\n"
        "providers:\n"
        "  built-in:\n    name: built-in\n    base_url: https://built-in\n"
        "mcp_servers:\n  drop: {command: npx}\n"
    )

    opencode_records = read_provider_records(tmp_path, "opencode")
    openclaw_records = read_provider_records(tmp_path, "openclaw")
    hermes_records = read_provider_records(tmp_path, "hermes")

    assert [row["provider_id"] for row in opencode_records] == ["one", "two"]
    assert [row["provider_id"] for row in openclaw_records] == ["one", "two"]
    assert [row["provider_id"] for row in hermes_records] == ["custom-one", "built-in"]
    assert next(row for row in openclaw_records if row["provider_id"] == "two")["is_current"] is True
    built_in = next(row for row in hermes_records if row["provider_id"] == "built-in")
    assert built_in["meta"]["native_read_only"] is True
    assert built_in["summary"]["editable"] is False
    assert built_in["summary"]["read_only_reason"] == "Hermes 原生 providers 配置由客户端维护"


def test_adapter_client_sets_match_supported_provider_clients() -> None:
    assert EXCLUSIVE_PROVIDER_APPS == {"claude", "codex", "gemini", "grokbuild"}
    assert ADDITIVE_PROVIDER_APPS == {"opencode", "openclaw", "hermes"}


def test_legacy_routing_record_still_gets_visible_summary_during_migration() -> None:
    summary = summarize_provider(
        "codex",
        {
            "type": "codex",
            "routing": {
                "base_url": "https://legacy.example/v1",
                "api_format": "openai_responses",
                "auth_mode": "bearer",
                "api_key": "legacy-key",
                "model": "gpt-legacy",
            },
        },
        {},
    )

    assert summary["base_url"] == "https://legacy.example/v1"
    assert summary["model"] == "gpt-legacy"
    assert summary["has_credentials"] is True


def test_additive_provider_meta_round_trips_router_protocol_and_auth_mode() -> None:
    settings = {
        "npm": "@ai-sdk/openai-compatible",
        "options": {"baseURL": "https://relay.example/v1"},
        "models": {"gpt-5.6": {"name": "GPT 5.6"}},
    }
    meta = {"api_format": "openai_responses", "auth_mode": "none"}

    form = build_provider_form("opencode", settings, meta)
    runtime = build_runtime_profile("opencode", settings, meta)

    assert form["api_format"] == "openai_responses"
    assert form["auth_mode"] == "none"
    assert runtime["api_format"] == "openai_responses"
    assert runtime["auth_mode"] == "none"
