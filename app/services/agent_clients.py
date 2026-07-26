from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentClientDefinition:
    id: str
    name: str
    package: str | None
    binary_names: tuple[str, ...]
    mcp_path: str | None
    prompt_file: str | None
    skill_dir: str | None
    config_paths: tuple[str, ...]
    detection_paths: tuple[str, ...]
    features: tuple[str, ...]
    write_support: tuple[str, ...]
    route_path: str | None = None


AGENT_CLIENTS: tuple[AgentClientDefinition, ...] = (
    AgentClientDefinition(
        id="claude",
        name="Claude Code",
        package="@anthropic-ai/claude-code",
        binary_names=("claude",),
        mcp_path="~/.claude.json",
        prompt_file="~/.claude/CLAUDE.md",
        skill_dir="~/.claude/skills",
        config_paths=("~/.claude.json", "~/.claude/CLAUDE.md", "~/.claude/skills"),
        detection_paths=("~/.claude.json", "~/.claude"),
        features=("providers", "route", "mcp", "skills", "prompts"),
        write_support=("providers", "route", "mcp", "skills", "prompts"),
        route_path="/claude",
    ),
    AgentClientDefinition(
        id="claude-desktop",
        name="Claude Desktop",
        package=None,
        binary_names=(),
        mcp_path="~/.config/Claude/claude_desktop_config.json",
        prompt_file=None,
        skill_dir=None,
        config_paths=("~/.config/Claude/claude_desktop_config.json",),
        detection_paths=("~/.config/Claude/claude_desktop_config.json",),
        features=("providers", "route", "mcp"),
        write_support=(),
        route_path="/claude-desktop",
    ),
    AgentClientDefinition(
        id="codex",
        name="Codex",
        package="@openai/codex",
        binary_names=("codex",),
        mcp_path="~/.codex/config.toml",
        prompt_file="~/.codex/AGENTS.md",
        skill_dir="~/.codex/skills",
        config_paths=("~/.codex/config.toml", "~/.codex/AGENTS.md", "~/.codex/skills"),
        detection_paths=("~/.codex/config.toml", "~/.codex"),
        features=("providers", "route", "mcp", "skills", "prompts"),
        write_support=("providers", "route", "mcp", "skills", "prompts"),
        route_path="/codex/v1",
    ),
    AgentClientDefinition(
        id="gemini",
        name="Gemini CLI",
        package="@google/gemini-cli",
        binary_names=("gemini",),
        mcp_path="~/.gemini/settings.json",
        prompt_file="~/.gemini/GEMINI.md",
        skill_dir="~/.gemini/skills",
        config_paths=("~/.gemini/settings.json", "~/.gemini/.env", "~/.gemini/GEMINI.md", "~/.gemini/skills"),
        detection_paths=("~/.gemini/settings.json", "~/.gemini"),
        features=("providers", "route", "mcp", "skills", "prompts"),
        write_support=("providers", "route", "mcp", "skills", "prompts"),
        route_path="/gemini",
    ),
    AgentClientDefinition(
        id="grokbuild",
        name="Grok Build",
        package=None,
        binary_names=("grok", "grok-build"),
        mcp_path="~/.grok/config.toml",
        prompt_file="~/.grok/AGENTS.md",
        skill_dir="~/.grok/skills",
        config_paths=("~/.grok/config.toml", "~/.grok/AGENTS.md", "~/.grok/skills"),
        detection_paths=("~/.grok/config.toml", "~/.grok"),
        features=("providers", "route", "mcp", "skills", "prompts"),
        write_support=(),
        route_path="/grokbuild/v1",
    ),
    AgentClientDefinition(
        id="opencode",
        name="OpenCode",
        package=None,
        binary_names=("opencode",),
        mcp_path="~/.config/opencode/opencode.json",
        prompt_file="~/.config/opencode/AGENTS.md",
        skill_dir="~/.config/opencode/skills",
        config_paths=("~/.config/opencode/opencode.json", "~/.config/opencode/AGENTS.md", "~/.config/opencode/skills"),
        detection_paths=("~/.config/opencode/opencode.json", "~/.config/opencode"),
        features=("providers", "route", "mcp", "skills", "prompts"),
        write_support=("providers", "route", "mcp", "skills", "prompts"),
        route_path="/opencode/v1",
    ),
    AgentClientDefinition(
        id="openclaw",
        name="OpenClaw",
        package="@qingchencloud/openclaw-zh",
        binary_names=("openclaw",),
        mcp_path="~/.openclaw/openclaw.json",
        prompt_file="~/.openclaw/AGENTS.md",
        skill_dir="~/.openclaw/skills",
        config_paths=("~/.openclaw/openclaw.json", "~/.openclaw/AGENTS.md", "~/.openclaw/skills"),
        detection_paths=("~/.openclaw/openclaw.json", "~/.openclaw"),
        features=("providers", "route", "mcp", "skills", "prompts"),
        write_support=("providers", "route", "mcp", "skills", "prompts"),
        route_path="/openclaw/v1",
    ),
    AgentClientDefinition(
        id="hermes",
        name="Hermes",
        package=None,
        binary_names=("hermes", "hermes-cli"),
        mcp_path="~/.hermes/config.yaml",
        prompt_file="~/.hermes/AGENTS.md",
        skill_dir="~/.hermes/skills",
        config_paths=("~/.hermes/config.yaml", "~/.hermes/AGENTS.md", "~/.hermes/skills"),
        detection_paths=("~/.hermes/config.yaml", "~/.hermes"),
        features=("providers", "mcp", "skills", "prompts"),
        write_support=("providers", "mcp", "skills", "prompts"),
    ),
)


def get_agent_client(client_id: str) -> AgentClientDefinition | None:
    return next((client for client in AGENT_CLIENTS if client.id == client_id), None)


def agent_clients_payload(
    home: Path | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> list[dict[str, object]]:
    home = home or Path.home()
    rows: list[dict[str, object]] = []
    for client in AGENT_CLIENTS:
        paths = [_resolve_agent_path(path, home) for path in client.config_paths]
        detection_paths = [
            _resolve_agent_path(path, home) for path in client.detection_paths
        ]
        config_detected = any(
            path.exists() for path in detection_paths if path is not None
        )
        binary = next(
            (
                resolved
                for name in client.binary_names
                if (resolved := which(name)) is not None
            ),
            None,
        )
        detected = config_detected or binary is not None
        detection_source = "config" if config_detected else "binary" if binary else None
        features = list(client.features)
        rows.append(
            {
                "id": client.id,
                "name": client.name,
                "package": client.package,
                "binary_names": list(client.binary_names),
                "binary_path": binary,
                "mcp_path": client.mcp_path,
                "prompt_file": client.prompt_file,
                "skill_dir": client.skill_dir,
                "route_path": client.route_path,
                "features": features,
                "capabilities": {feature: True for feature in features},
                "write_support": list(client.write_support),
                "detected": detected,
                "detection_source": detection_source,
                "paths": [str(path) for path in paths if path is not None],
            }
        )
    return rows


def _resolve_agent_path(value: str, home: Path) -> Path | None:
    if " / " in value:
        return None
    if value.startswith("~/"):
        return home / value[2:]
    if value in {"AGENTS.md", "SOUL.md"}:
        return home / value
    return Path(value)
