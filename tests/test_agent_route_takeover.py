from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.agent_route_takeover import AgentRouteTakeover, RouteTakeoverError


def test_codex_takeover_preserves_config_and_restores(tmp_path: Path) -> None:
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    original = (
        'model = "keep"\n'
        'model_provider = "original"\n'
        '[model_providers.original]\n'
        'base_url = "https://relay.example/v1"\n'
    )
    config.write_text(original, encoding="utf-8")
    manager = AgentRouteTakeover(tmp_path, tmp_path / "state")

    enabled = manager.enable("codex", "http://127.0.0.1:7888/codex/v1")
    assert enabled["verified"] is True
    assert 'model = "keep"' in config.read_text(encoding="utf-8")
    assert "127.0.0.1:7888" in config.read_text(encoding="utf-8")

    restored = manager.disable("codex")
    assert restored["verified"] is True
    assert config.read_text(encoding="utf-8") == original
    assert "https://relay.example/v1" in config.read_text(encoding="utf-8")


def test_codex_restore_does_not_clobber_external_edit(tmp_path: Path) -> None:
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        'model_provider = "original"\n[model_providers.original]\nbase_url = "https://old"\n',
        encoding="utf-8",
    )
    manager = AgentRouteTakeover(tmp_path, tmp_path / "state")
    manager.enable("codex", "http://127.0.0.1:7888/codex/v1")
    config.write_text(
        'model_provider = "router"\nmodel = "changed-outside"\n'
        '[model_providers.router]\nbase_url = "http://127.0.0.1:7888/codex/v1"\n',
        encoding="utf-8",
    )

    result = manager.disable("codex")
    restored = config.read_text(encoding="utf-8")

    assert result["external_change"] is True
    assert 'model = "changed-outside"' in restored
    assert "127.0.0.1:7888" not in restored
    assert 'model_provider = "original"' in restored


def test_json_client_takeover_writes_and_restores_owned_field(tmp_path: Path) -> None:
    config = tmp_path / ".openclaw/openclaw.json"
    config.parent.mkdir(parents=True)
    original = {"model": "keep", "baseUrl": "https://old.example", "other": {"x": 1}}
    config.write_text(json.dumps(original), encoding="utf-8")
    manager = AgentRouteTakeover(tmp_path, tmp_path / "state")

    manager.enable("openclaw", "http://127.0.0.1:7888/openclaw/v1")
    enabled = json.loads(config.read_text(encoding="utf-8"))
    assert enabled["baseUrl"] == "http://127.0.0.1:7888/openclaw/v1"
    manager.disable("openclaw")
    assert json.loads(config.read_text(encoding="utf-8")) == original


def test_unsupported_client_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(RouteTakeoverError, match="unsupported"):
        AgentRouteTakeover(tmp_path, tmp_path / "state").enable(
            "hermes", "http://127.0.0.1:7888/hermes"
        )
