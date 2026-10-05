from __future__ import annotations

import os
import pwd
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class AgentResolvedPaths:
    """One client's effective filesystem contract for the current WSL home."""

    client_id: str
    root: Path
    config: Path | None
    mcp: Path | None
    prompt: Path | None
    skills: Path | None
    route: Path | None
    detection: tuple[Path, ...]
    config_paths: tuple[Path, ...]


# The names mirror the per-client directory overrides used by CC Switch. They
# are intentionally read at operation time: a process can launch a client with
# a different profile without requiring a panel restart.
_ENV_ROOTS = {
    "claude": "CLAUDE_CONFIG_DIR",
    "claude-desktop": "CLAUDE_DESKTOP_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "gemini": "GEMINI_CLI_HOME",
    "grokbuild": "GROK_HOME",
    "opencode": "OPENCODE_CONFIG_DIR",
    "openclaw": "OPENCLAW_HOME",
    "hermes": "HERMES_HOME",
}

_DEFAULT_ROOTS = {
    "claude": ".claude",
    "claude-desktop": ".config/Claude",
    "codex": ".codex",
    "gemini": ".gemini",
    "grokbuild": ".grok",
    "opencode": ".config/opencode",
    "openclaw": ".openclaw",
    "hermes": ".hermes",
}

# Relative paths are used when an explicit client root is supplied. Claude
# Code's legacy MCP file is the one exception: its default lives beside the
# root, while an override follows the override directory like CC Switch.
_LAYOUT: dict[str, dict[str, str | None]] = {
    "claude": {
        "config": ".claude.json",
        "mcp": ".claude.json",
        "prompt": "CLAUDE.md",
        "skills": "skills",
        "route": ".claude.json",
    },
    "claude-desktop": {
        "config": "claude_desktop_config.json",
        "mcp": "claude_desktop_config.json",
        "prompt": None,
        "skills": None,
        "route": None,
    },
    "codex": {
        "config": "config.toml",
        "mcp": "config.toml",
        "prompt": "AGENTS.md",
        "skills": "skills",
        "route": "config.toml",
    },
    "gemini": {
        "config": "settings.json",
        "mcp": "settings.json",
        "prompt": "GEMINI.md",
        "skills": "skills",
        "route": ".env",
    },
    "grokbuild": {
        "config": "config.toml",
        "mcp": "config.toml",
        "prompt": "AGENTS.md",
        "skills": "skills",
        "route": "config.toml",
    },
    "opencode": {
        "config": "opencode.json",
        "mcp": "opencode.json",
        "prompt": "AGENTS.md",
        "skills": "skills",
        "route": "opencode.json",
    },
    "openclaw": {
        "config": "openclaw.json",
        "mcp": None,
        "prompt": "AGENTS.md",
        "skills": None,
        "route": "openclaw.json",
    },
    "hermes": {
        "config": "config.yaml",
        "mcp": "config.yaml",
        "prompt": "AGENTS.md",
        "skills": "skills",
        "route": None,
    },
}


def _system_home() -> Path:
    """Return the account Home independently of a temporarily patched HOME."""
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir).expanduser()
    except (KeyError, ImportError, OSError):
        return Path.home()


def _is_isolated_home(home: Path) -> bool:
    try:
        return home.expanduser().resolve() != _system_home().resolve()
    except OSError:
        return home.expanduser() != _system_home()


def resolve_agent_paths(
    client_id: str,
    home: Path | str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    overrides: Mapping[str, Path | str] | None = None,
) -> AgentResolvedPaths:
    client = str(client_id).strip().lower()
    if client not in _DEFAULT_ROOTS:
        raise ValueError(f"unknown agent client: {client_id}")
    home_path = Path(home) if home is not None else Path.home()
    environment = os.environ if environ is None else environ
    override_value = (overrides or {}).get(client)
    raw_root = override_value
    if raw_root is None and environ is None and _is_isolated_home(home_path):
        # A caller that supplies a fixture/alternate Home expects the client's
        # default layout under that Home.  Ambient process variables such as
        # CODEX_HOME belong to the panel's real login profile and must not
        # redirect a scan or write into that profile.  Explicit ``environ``
        # or ``overrides`` remains the opt-in escape hatch for relocated
        # client profiles (the same contract used by CC Switch settings).
        raw_root = None
    elif raw_root is None:
        env_name = _ENV_ROOTS.get(client)
        raw_root = environment.get(env_name) if env_name else None
    if raw_root is None or not str(raw_root).strip():
        root = home_path / _DEFAULT_ROOTS[client]
        explicit_root = False
    else:
        root = Path(str(raw_root).strip()).expanduser()
        if not root.is_absolute():
            root = home_path / root
        explicit_root = True

    layout = _LAYOUT[client]

    def under_root(key: str) -> Path | None:
        relative = layout[key]
        if relative is None:
            return None
        return root / relative

    if client == "claude" and not explicit_root:
        # Preserve Claude Code's documented default ~/.claude.json location.
        mcp = home_path / ".claude.json"
        config = mcp
        route = mcp
    else:
        mcp = under_root("mcp")
        config = under_root("config")
        route = under_root("route")

    prompt = under_root("prompt")
    skills = under_root("skills")
    config_paths = tuple(
        path
        for path in (config, prompt, skills)
        if path is not None
    )
    detection = tuple(dict.fromkeys((*config_paths, root)))
    return AgentResolvedPaths(
        client_id=client,
        root=root,
        config=config,
        mcp=mcp,
        prompt=prompt,
        skills=skills,
        route=route,
        detection=detection,
        config_paths=config_paths,
    )
