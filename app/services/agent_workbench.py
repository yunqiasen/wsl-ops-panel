from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.services.agent_provider_projection import provider_matches
from app.services.agent_clients import agent_clients_payload
from app.services.agent_mcp import AgentMcpStore, agent_data_root, public_mcp_server
from app.services.agent_mcp_adapters import (
    CLIENT_PATHS,
)
from app.services.agent_profiles import AgentProfileStore
from app.services.agent_provider_adapters import ADDITIVE_PROVIDER_APPS
from app.services.agent_prompts import AgentPromptStore
from app.services.agent_router_config import AgentRouterConfigStore
from app.services.agent_router_control import AgentRouterController
from app.services.agent_providers import (
    AgentProviderStore,
    public_provider,
    redact_sensitive,
)
from app.services.agent_skill_store import AgentSkillStore
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
    provider_store = AgentProviderStore(data_root)
    providers = provider_store.list_providers()
    provider_rows = _provider_public_rows(provider_store, providers, home)
    clients = (
        agent_clients_payload(home)
        if which is None
        else agent_clients_payload(home, which=which)
    )
    state = PanelStateStore(data_root)
    mcp_store = AgentMcpStore(data_root)
    managed = mcp_store.list_servers()
    scan_errors: dict[str, dict[str, str]] = {}
    observations_by_mcp: dict[str, dict[str, dict[str, Any]]] = {}

    for client in clients:
        client_id = str(client["id"])
        if client_id not in CLIENT_PATHS:
            continue
        try:
            observations = mcp_store.refresh_observations(home, client_id)
            for observation in observations:
                observations_by_mcp.setdefault(observation['mcp_id'], {})[client_id] = observation
        except Exception as exc:
            # Parsing errors are visible as client scan errors, never as installed state.
            scan_errors[client_id] = {
                "status": "error",
                "error": str(exc),
            }

    observations_by_target: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
    installed_by_target: dict[str, dict[str, list[str]]] = {}
    for observation in state.list_mcp_observations():
        observations_by_target.setdefault(observation["mcp_id"], {}).setdefault(
            observation["node_id"], {}
        )[observation["client_id"]] = observation
        if observation.get("present") and observation.get("status") != "error":
            installed_by_target.setdefault(observation["node_id"], {}).setdefault(
                observation["client_id"], []
            ).append(observation["mcp_id"])

    for client_map in installed_by_target.values():
        for mcp_ids in client_map.values():
            mcp_ids.sort(key=str.lower)

    rows: dict[str, dict[str, Any]] = {}
    for mcp_id, server in managed.items():
        row = public_mcp_server(server)
        row["managed"] = True
        row["observations"] = observations_by_mcp.get(mcp_id, {})
        row["observations_by_target"] = observations_by_target.get(mcp_id, {})
        row["status"] = "installed" if row["observations_by_target"] else "unscanned"
        rows[mcp_id] = row
    discovery: dict[str, dict[str, Any]] = {}
    for mcp_id, target_observations in observations_by_target.items():
        if mcp_id in rows:
            continue
        local_observations = target_observations.get("__local__", {})
        first_target = next(iter(target_observations.values()))
        first = next(iter(first_target.values()))
        discovery[mcp_id] = {
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
            "observations": local_observations,
            "observations_by_target": target_observations,
            "status": "observed",
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
    for row in [*rows.values(), *discovery.values()]:
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
    # 有真实配置的客户端优先于仅凭 PATH 二进制命中的客户端，避免页面默认
    # 落到一个没有可读配置的应用，进而把真实 MCP 显示成“未扫描”。
    config_detected = [
        client for client in detected_clients if client.get("detection_source") == "config"
    ]
    active_pool = config_detected or detected_clients
    active_client = str(active_pool[0]["id"]) if active_pool else None
    router_store = AgentRouterConfigStore(data_root)
    router_snapshot = router_store.public_snapshot()
    # The takeover record owns the live file. Merge it with the persisted flag
    # so a partial enable/disable cannot make the UI offer conflicting writes.
    try:
        active_takeovers = AgentRouterController(
            router_store, home=home
        ).active_takeover_clients()
    except (OSError, ValueError):
        active_takeovers = {
            str(client_id)
            for client_id, enabled in dict(
                router_snapshot.get("takeover") or {}
            ).items()
            if bool(enabled)
        }
    router_snapshot["takeover"] = {
        client_id: True for client_id in sorted(active_takeovers)
    }

    skill_store = AgentSkillStore(data_root)
    prompt_store = AgentPromptStore(data_root)
    for client in clients:
        client_id = str(client.get("id") or "")
        write_support = set(client.get("write_support") or [])
        if "skills" in write_support:
            skill_store.refresh_client_observations(home, client_id)
        if "prompts" in write_support:
            prompt_store.refresh_client_observation(home, client_id)
    managed_skills = skill_store.list()
    managed_prompts = prompt_store.list_prompts()
    mcp_assignments = _group_rows(state.list_mcp_assignments(), "mcp_id")
    mcp_observations = _group_rows(state.list_mcp_observations(), "mcp_id")
    skill_assignments = _group_rows(state.list_skill_assignments(), "skill_id")
    skill_observations = _group_rows(state.list_skill_observations(), "skill_id")
    prompt_assignments = _group_rows(state.list_prompt_assignments(), "prompt_id")
    prompt_observations = _group_rows(state.list_prompt_observations(), "prompt_id")
    mcp_library: list[dict[str, Any]] = []
    for mcp_id, server in managed.items():
        variants = []
        for variant in state.list_mcp_variants(mcp_id):
            safe_variant = dict(variant)
            safe_variant.pop("spec_json", None)
            safe_variant["spec"] = redact_sensitive(
                dict(variant.get("spec") or {})
            )
            variants.append(safe_variant)
        mcp_library.append(
            {
                **public_mcp_server(server),
                "variants": variants,
                "assignments": mcp_assignments.get(mcp_id, []),
                "observations": [
                    _public_observation(item)
                    for item in mcp_observations.get(mcp_id, [])
                ],
            }
        )

    skill_library = [
        {
            **skill,
            "variants": state.list_skill_variants(skill_id),
            "assignments": skill_assignments.get(skill_id, []),
            "observations": skill_observations.get(skill_id, []),
        }
        for skill_id, skill in managed_skills.items()
    ]
    prompt_library = [
        {
            **prompt,
            "variants": state.list_prompt_variants(prompt_id),
            "assignments": prompt_assignments.get(prompt_id, []),
            "observations": prompt_observations.get(prompt_id, []),
        }
        for prompt_id, prompt in managed_prompts.items()
    ]
    prompt_discovery = _prompt_discovery(
        home,
        detected_clients,
        state.list_prompt_assignments(),
        managed_prompts,
    )
    library = {
        "mcp": mcp_library,
        "skills": skill_library,
        "prompts": prompt_library,
        "providers": [
            provider
            for items in provider_rows.values()
            for provider in items
        ],
        "profiles": [
            redact_sensitive(profile)
            for profile in AgentProfileStore(data_root).list()
        ],
        "router": router_snapshot,
        "discovery": {
            "mcp": sorted(
                discovery.values(),
                key=lambda item: (str(item["name"]).lower(), str(item["id"])),
            ),
            "skills": _skill_discovery(home, managed_skills),
            "prompts": prompt_discovery,
        },
    }

    return {
        "agent_clients": clients,
        "agent_detected_clients": detected_clients,
        "agent_supported_clients": clients,
        "agent_active_client": active_client,
        "agent_router_config": router_snapshot,
        "agent_router_takeover": router_snapshot.get("takeover", {}),
        "agent_mcp_servers": list(rows.values()),
        "agent_mcp_discovery": sorted(
            discovery.values(),
            key=lambda row: (str(row["name"]).lower(), str(row["id"])),
        ),
        "agent_providers": provider_rows,
        "agent_prompts": list(managed_prompts.values()),
        "agent_skills": scan_agent_skills(home),
        "agent_library": library,
        "agent_profiles": library["profiles"],
        "agent_supported_apps": [client["id"] for client in clients],
        "agent_provider_apps": [client["id"] for client in clients],
        "agent_mcp_installed_by_target": installed_by_target,
        "agent_scan_errors": scan_errors,
    }


def _provider_public_rows(
    store: AgentProviderStore,
    providers: dict[str, list[dict[str, Any]]],
    home: Path,
) -> dict[str, list[dict[str, Any]]]:
    active = AgentRouterController(AgentRouterConfigStore(store.data_root), home=home).active_takeover_clients()
    rows: dict[str, list[dict[str, Any]]] = {}
    for app_id, items in providers.items():
        live_ids: set[str] = set()
        live_state_known = True
        if app_id in ADDITIVE_PROVIDER_APPS:
            try:
                live_ids = store.live_provider_ids(app_id, home)
            except (OSError, ValueError):
                live_state_known = False
        public_rows: list[dict[str, Any]] = []
        for provider in items:
            row = public_provider(provider)
            if app_id in ADDITIVE_PROVIDER_APPS:
                row["live_state_known"] = live_state_known
                row["live_state"] = (
                    "added"
                    if live_state_known and str(row.get("id") or "") in live_ids
                    else "saved"
                    if live_state_known
                    else "unknown"
                )
            else:
                row["live_state_known"] = True
                row["live_state"] = "saved"
                if row.get("is_current"):
                    try:
                        matches = app_id in active or provider_matches(home, provider, write_secrets=False)
                        row["live_state"] = "current" if matches else "drifted"
                        row["is_current"] = matches
                    except (OSError, ValueError):
                        row.update(live_state="unknown", live_state_known=False, is_current=False)
            public_rows.append(row)
        rows[app_id] = public_rows
    return rows


def _group_rows(
    rows: list[dict[str, Any]], resource_key: str
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row[resource_key]), []).append(row)
    return grouped


def _public_observation(item: dict[str, Any]) -> dict[str, Any]:
    safe = dict(item)
    safe.pop("public_spec_json", None)
    if isinstance(safe.get("public_spec"), dict):
        safe["public_spec"] = redact_sensitive(dict(safe["public_spec"]))
    return safe


def _skill_discovery(
    home: Path, managed: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for client_id, inventory in scan_agent_skills(home).items():
        items = inventory.get("items") if isinstance(inventory, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            skill_id = str(item.get("name") or "")
            if not skill_id or skill_id in managed:
                continue
            rows.append({**item, "id": skill_id, "client_id": client_id})
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("name") or "").lower(),
            str(item.get("client_id") or ""),
        ),
    )


def _prompt_discovery(
    home: Path,
    clients: list[dict[str, Any]],
    assignments: list[dict[str, Any]],
    managed: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    assigned_clients = {
        str(item["client_id"])
        for item in assignments
        if item.get("node_id") == "__local__" and item.get("desired_enabled", True)
    }
    rows: list[dict[str, Any]] = []
    for client in clients:
        client_id = str(client.get("id") or "")
        prompt_path = client.get("prompt_file")
        if (
            not client_id
            or client_id in assigned_clients
            or "prompts" not in set(client.get("write_support") or [])
            or not prompt_path
        ):
            continue
        target = Path(str(prompt_path))
        if not target.is_absolute():
            target = home / str(prompt_path).removeprefix("~/")
        resource_id = f"{client_id}-current"
        if not target.is_file() or resource_id in managed:
            continue
        rows.append(
            {
                "id": resource_id,
                "name": f"{client.get('name') or client_id} 当前提示词",
                "client_id": client_id,
                "path": str(target),
                "source": "observed",
                "status": "observed",
            }
        )
    return rows
