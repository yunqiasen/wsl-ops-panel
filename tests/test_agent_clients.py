from pathlib import Path

from app.services.agent_clients import AGENT_CLIENTS, agent_clients_payload
from app.services.agent_workbench import build_agent_workbench_context


EXPECTED_CLIENT_IDS = {
    "claude",
    "claude-desktop",
    "codex",
    "gemini",
    "grokbuild",
    "opencode",
    "openclaw",
    "hermes",
}


def test_registry_contains_all_eight_clients_and_declares_route_capability() -> None:
    by_id = {item.id: item for item in AGENT_CLIENTS}

    assert set(by_id) == EXPECTED_CLIENT_IDS
    assert "providers" in by_id["codex"].features
    assert "route" in by_id["codex"].features
    assert "route" in by_id["claude"].features
    assert "route" not in by_id["hermes"].features
    assert by_id["claude-desktop"].binary_names == ()
    assert by_id["grokbuild"].route_path == "/grokbuild/v1"


def test_payload_uses_real_paths_or_binaries_and_keeps_detection_separate(
    tmp_path: Path,
) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("", encoding="utf-8")
    payload = agent_clients_payload(tmp_path, which=lambda _: None)

    assert [row["id"] for row in payload if row["detected"]] == ["codex"]
    codex = next(row for row in payload if row["id"] == "codex")
    assert codex["detection_source"] == "config"
    assert codex["route_path"] == "/codex/v1"
    assert codex["capabilities"]["route"] is True


def test_workbench_context_exposes_only_detected_clients_as_main_switcher(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text("", encoding="utf-8")
    config_root = tmp_path / "config"
    config_root.mkdir()

    context = build_agent_workbench_context(config_root, home=home, which=lambda _: None)

    assert [row["id"] for row in context["agent_detected_clients"]] == ["codex"]
    assert len(context["agent_supported_clients"]) == 8
    assert context["agent_active_client"] == "codex"


def test_grokbuild_and_hermes_declare_verified_write_capabilities() -> None:
    by_id = {item.id: item for item in AGENT_CLIENTS}

    assert set(by_id["grokbuild"].write_support) == {
        "providers",
        "route",
        "mcp",
        "skills",
        "prompts",
    }
    assert set(by_id["hermes"].write_support) == {
        "providers",
        "mcp",
        "skills",
        "prompts",
    }
