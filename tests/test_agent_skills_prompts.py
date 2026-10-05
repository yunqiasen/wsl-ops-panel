from __future__ import annotations

import json
import zipfile
from pathlib import Path


def create_skill(path: Path, content: str = "# Demo\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(content, encoding="utf-8")


def build_skill_zip(root: Path) -> Path:
    source = root / "archive-source"
    create_skill(source, "# From ZIP\n")
    archive = root / "demo.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.write(source / "SKILL.md", "demo/SKILL.md")
    return archive


def test_skill_install_supports_zip_copy_and_symlink(tmp_path: Path) -> None:
    from app.services.agent_skills import (
        install_skill_to_home,
        uninstall_skill_from_home,
    )

    archive = build_skill_zip(tmp_path)
    installed = install_skill_to_home(
        tmp_path / "home", "codex", "demo", str(archive), mode="copy"
    )
    assert installed["verified"] is True
    assert (tmp_path / "home/.codex/skills/demo/SKILL.md").exists()
    metadata = json.loads(
        (tmp_path / "home/.codex/skills/demo/.wsl-ops-skill.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["mode"] == "copy"
    from app.services.agent_skills import scan_agent_skills

    scanned = scan_agent_skills(tmp_path / "home")
    assert scanned["codex"]["items"][0]["source"] == str(archive)
    assert scanned["codex"]["items"][0]["mode"] == "copy"

    source = tmp_path / "source"
    create_skill(source)
    linked = install_skill_to_home(
        tmp_path / "home", "claude", "linked", str(source), mode="symlink"
    )
    assert linked["verified"] is True
    assert (tmp_path / "home/.claude/skills/linked").is_symlink()
    assert (tmp_path / "home/.claude/skills/linked/SKILL.md").exists()

    removed = uninstall_skill_from_home(tmp_path / "home", "codex", "demo")
    assert removed["verified"] is True
    assert not (tmp_path / "home/.codex/skills/demo").exists()
    assert removed["backup_path"]
    assert Path(removed["backup_path"]).exists()


def test_skill_install_rejects_unsafe_zip_and_missing_entrypoint(tmp_path: Path) -> None:
    from app.services.agent_skills import install_skill_to_home

    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../../escape.txt", "bad")
    try:
        install_skill_to_home(
            tmp_path / "home", "codex", "unsafe", str(archive), mode="copy"
        )
    except ValueError as exc:
        assert "ZIP" in str(exc)
    else:
        raise AssertionError("unsafe archive must be rejected")


def _write_agent_category(root: Path) -> None:
    (root / "categories").mkdir(parents=True, exist_ok=True)
    (root / "categories" / "agent.yaml").write_text(
        "id: agent\nlabel: Agent\norder: 60\nenabled: true\n", encoding="utf-8"
    )
    (root / "objects").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    (root / "rules" / "node-packages.yaml").write_text("packages: []\n", encoding="utf-8")
    (root / "rules" / "python-packages.yaml").write_text("packages: []\n", encoding="utf-8")


def test_local_skill_api_installs_updates_and_uninstalls_one_client(
    tmp_path: Path, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    source = tmp_path / "source-api"
    create_skill(source, "# V1\n")
    monkeypatch.setenv("HOME", str(home))
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    installed = client.post(
        "/api/agent/skills/local/install",
        json={
            "client_id": "codex",
            "skill_name": "demo",
            "source": str(source),
            "mode": "copy",
        },
    )
    assert installed.status_code == 200
    assert installed.json()["verified"] is True
    from app.services.agent_skill_store import AgentSkillStore

    skill_store = AgentSkillStore(tmp_path / "data/agent")
    assert skill_store.get("demo") is not None
    assert skill_store.state.list_skill_assignments("__local__", "codex")[0]["skill_id"] == "demo"

    (source / "SKILL.md").write_text("# V2\n", encoding="utf-8")
    updated = client.post(
        "/api/agent/skills/local/update",
        json={"client_id": "codex", "skill_name": "demo"},
    )
    assert updated.status_code == 200
    assert updated.json()["updated"] == ["demo"]
    assert (home / ".codex/skills/demo/SKILL.md").read_text(encoding="utf-8") == "# V2\n"

    removed = client.post(
        "/api/agent/skills/local/uninstall",
        json={"client_id": "codex", "skill_name": "demo"},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] == ["demo"]
    assert Path(removed.json()["backup_path"]).exists()
    assert skill_store.get("demo") is not None
    assert skill_store.state.list_skill_assignments("__local__", "codex") == []


def test_prompt_import_apply_and_restore(tmp_path: Path) -> None:
    from app.services.agent_prompts import AgentPromptFileManager

    home = tmp_path / "home"
    target = home / ".codex/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("original\n", encoding="utf-8")
    manager = AgentPromptFileManager(home, tmp_path / "state")

    assert manager.import_current("codex")["content"] == "original\n"
    applied = manager.apply("codex", "replacement\n")
    assert applied["verified"] is True
    assert target.read_text(encoding="utf-8") == "replacement\n"
    restored = manager.restore("codex")
    assert restored["verified"] is True
    assert target.read_text(encoding="utf-8") == "original\n"
    assert (tmp_path / "state/codex.json").stat().st_mode & 0o777 == 0o600


def test_prompt_local_api_imports_applies_template_and_restores(
    tmp_path: Path, monkeypatch
) -> None:
    from fastapi.testclient import TestClient

    from app.core.security import COOKIE_NAME, issue_session_token
    from tests.app_factory import create_app
    from app.services.agent_prompts import AgentPromptStore

    _write_agent_category(tmp_path)
    home = tmp_path / "home"
    target = home / ".codex/AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("before\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    AgentPromptStore(tmp_path / "data/agent").upsert_prompt(
        "default", "默认", "after\n"
    )
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: []))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    imported = client.post(
        "/api/agent/prompts/import-current", json={"client_id": "codex"}
    )
    assert imported.status_code == 200
    assert imported.json()["content"] == "before\n"

    applied = client.post(
        "/api/agent/prompts/local/apply",
        json={"client_id": "codex", "prompt_id": "default"},
    )
    assert applied.status_code == 200
    assert applied.json()["verified"] is True
    assert target.read_text(encoding="utf-8") == "after\n"
    prompt_store = AgentPromptStore(tmp_path / "data/agent")
    assert prompt_store.state.list_prompt_assignments("__local__", "codex")[0]["prompt_id"] == "default"

    restored = client.post(
        "/api/agent/prompts/local/restore", json={"client_id": "codex"}
    )
    assert restored.status_code == 200
    assert target.read_text(encoding="utf-8") == "before\n"
    assert prompt_store.state.list_prompt_assignments("__local__", "codex") == []
