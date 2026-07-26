from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.agent_clients import agent_clients_payload
from app.services.agent_mcp import AgentMcpStore, agent_data_root, public_mcp_server
from app.services.agent_mcp_adapters import CLIENT_PATHS, scan_mcp_home
from app.services.agent_prompts import AgentPromptStore
from app.services.agent_router_config import AgentRouterConfigStore
from app.services.agent_providers import (
    AgentProviderStore,
    public_provider,
    redact_sensitive,
)
from app.services.agent_skills import scan_agent_skills
from app.services.state_store import PanelStateStore


def build_agent_workbench_context(
    config_root: Path | str,
    *,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    home = home or Path.home()
    data_root = agent_data_root(config_root)
    providers = AgentProviderStore(data_root).list_providers()
    clients = (
        agent_clients_payload(home)
        if which is None
        else agent_clients_payload(home, which=which)
    )
    state = PanelStateStore(data_root)
    observations_by_mcp: dict[str, dict[str, dict[str, Any]]] = {}

    for client in clients:
        client_id = str(client["id"])
        if client_id not in CLIENT_PATHS:
            continue
        try:
            scanned = scan_mcp_home(home, client_id)
            observations = []
            for mcp_id, spec in scanned.items():
                public_spec = redact_sensitive(spec)
                spec_hash = hashlib.sha256(
                    json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                observation = {
                    "mcp_id": mcp_id,
                    "present": True,
                    "spec_hash": spec_hash,
                    "public_spec": public_spec,
                    "status": "installed",
                }
                observations.append(observation)
                observations_by_mcp.setdefault(mcp_id, {})[client_id] = observation
            state.replace_mcp_observations("__local__", client_id, observations)
        except Exception as exc:
            # Parsing errors are visible as client scan errors, never as installed state.
            observations_by_mcp.setdefault("__scan__", {})[client_id] = {
                "status": "error",
                "error": str(exc),
            }

    observations_by_target: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    installed_by_target: dict[str, dict[str, list[str]]] = {}
    for observation in state.list_mcp_observations():
        observations_by_target.setdefault(observation["mcp_id"], {}).setdefault(
            observation["node_id"], {}
        )[observation["client_id"]] = observation
        if observation.get("present") and observation.get("status") == "installed":
            installed_by_target.setdefault(observation["node_id"], {}).setdefault(
                observation["client_id"], []
            ).append(observation["mcp_id"])

    for client_map in installed_by_target.values():
        for mcp_ids in client_map.values():
            mcp_ids.sort(key=str.lower)

    managed = AgentMcpStore(data_root).list_servers()
    rows: dict[str, dict[str, Any]] = {}
    for mcp_id, server in managed.items():
        row = public_mcp_server(server)
        row["managed"] = True
        row["observations"] = observations_by_mcp.get(mcp_id, {})
        row["observations_by_target"] = observations_by_target.get(mcp_id, {})
        row["status"] = "installed" if row["observations_by_target"] else "unscanned"
        rows[mcp_id] = row
    for mcp_id, client_observations in observations_by_mcp.items():
        if mcp_id == "__scan__" or mcp_id in rows:
            continue
        first = next(iter(client_observations.values()))
        rows[mcp_id] = {
            "id": mcp_id,
            "name": mcp_id,
            "spec": first.get("public_spec", {}),
            "apps": {},
            "description": None,
            "homepage": None,
            "docs": None,
            "tags": [],
            "source": "observed",
            "managed": False,
            "observations": client_observations,
            "observations_by_target": observations_by_target.get(
                mcp_id, {"__local__": client_observations}
            ),
            "status": "installed",
        }

    for mcp_id, target_observations in observations_by_target.items():
        if mcp_id in rows:
            continue
        first_target = next(iter(target_observations.values()))
        first = next(iter(first_target.values()))
        rows[mcp_id] = {
            "id": mcp_id,
            "name": mcp_id,
            "spec": first.get("public_spec", {}),
            "apps": {},
            "description": None,
            "homepage": None,
            "docs": None,
            "tags": [],
            "source": "observed",
            "managed": False,
            "observations": target_observations.get("__local__", {}),
            "observations_by_target": target_observations,
            "status": "installed",
        }

    client_names = {str(client["id"]): str(client["name"]) for client in clients}
    preferred_client_order = [
        "codex",
        "claude",
        "gemini",
        "grokbuild",
        "opencode",
        "openclaw",
        "hermes",
        "claude-desktop",
    ]
    available_client_ids = {str(client["id"]) for client in clients}
    client_order = [
        client_id
        for client_id in preferred_client_order
        if client_id in available_client_ids
    ]
    for row in rows.values():
        summaries: list[dict[str, Any]] = []
        for node_id, client_map in sorted(
            row["observations_by_target"].items(),
            key=lambda item: (item[0] != "__local__", item[0].lower()),
        ):
            installed_clients = [
                client_id
                for client_id in client_order
                if client_id in client_map
                and client_map[client_id].get("present")
                and client_map[client_id].get("status") == "installed"
            ]
            if not installed_clients:
                continue
            names = [client_names.get(client_id, client_id) for client_id in installed_clients]
            summaries.append(
                {
                    "node_id": node_id,
                    "node_label": "当前 WSL" if node_id == "__local__" else node_id,
                    "client_ids": installed_clients,
                    "client_names": names,
                    "client_count": len(installed_clients),
                    "title": f"{'当前 WSL' if node_id == '__local__' else node_id} · 已装 {len(names)}：{' / '.join(names)}",
                }
            )
        row["target_summaries"] = summaries

    detected_clients = [client for client in clients if client.get("detected")]
    active_client = str(detected_clients[0]["id"]) if detected_clients else None
    router_snapshot = AgentRouterConfigStore(data_root).public_snapshot()

    return {
        "agent_clients": clients,
        "agent_detected_clients": detected_clients,
        "agent_supported_clients": clients,
        "agent_active_client": active_client,
        "agent_router_config": router_snapshot,
        "agent_router_takeover": router_snapshot.get("takeover", {}),
        "agent_mcp_servers": sorted(
            rows.values(), key=lambda row: (str(row["name"]).lower(), str(row["id"]))
        ),
        "agent_providers": {
            app_id: [public_provider(provider) for provider in items]
            for app_id, items in providers.items()
        },
        "agent_prompts": list(AgentPromptStore(data_root).list_prompts().values()),
        "agent_skills": scan_agent_skills(home),
        "agent_supported_apps": [client["id"] for client in clients],
        "agent_provider_apps": [client["id"] for client in clients],
        "agent_mcp_installed_by_target": installed_by_target,
        "agent_scan_errors": observations_by_mcp.get("__scan__", {}),
    }
