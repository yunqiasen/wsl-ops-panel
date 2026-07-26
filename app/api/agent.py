from __future__ import annotations

import hashlib
import json
from time import perf_counter
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import ActionPlan
from app.core.security import require_authenticated_request
from app.models.tasks import TaskRecord
from app.services.agent_mcp import (
    AgentMcpStore,
    agent_data_root,
    build_mcp_apply_shell,
    build_mcp_remove_shell,
    build_mcp_scan_shell,
    parse_mcp_scan_output,
    public_mcp_server,
)
from app.services.agent_mcp_adapters import scan_mcp_home
from app.services.agent_prompts import AgentPromptStore, build_prompt_apply_shell
from app.services.agent_providers import (
    AgentProviderStore,
    build_provider_apply_shell,
    public_provider,
    redact_sensitive,
)
from app.services.state_store import PanelStateStore
from app.services.agent_skills import (
    build_skill_delete_shell,
    build_skill_install_shell,
    build_skill_update_shell,
    safe_skill_name,
)

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentImportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imported_count: int


class AgentQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queued_count: int
    tasks: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class AgentClientSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apps: list[str] = Field(default_factory=list)


class AgentScanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_ids: list[str] = Field(default_factory=list)
    apps: list[str] = Field(default_factory=list)


class AgentProviderUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str
    provider_id: str
    name: str
    settings_config: dict[str, object] = Field(default_factory=dict)
    routing: dict[str, object] | None = None
    website_url: str | None = None
    category: str | None = None
    notes: str | None = None
    icon: str | None = None
    icon_color: str | None = None
    is_current: bool = False


class AgentProviderApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str
    provider_id: str
    node_ids: list[str]
    write_secrets: bool = False


class AgentProviderCurrentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str
    provider_id: str


class AgentMcpUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_id: str
    spec: dict[str, object]
    apps: dict[str, bool] = Field(default_factory=dict)
    name: str | None = None
    description: str | None = None
    homepage: str | None = None
    docs: str | None = None
    tags: list[str] = Field(default_factory=list)


class AgentMcpSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    server_ids: list[str]
    apps: list[str]
    node_ids: list[str]


class AgentMcpTargetAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    client_id: str
    mcp_ids: list[str]


class AgentMcpMatrixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["install", "uninstall"] = "install"
    assignments: list[AgentMcpTargetAssignment]


class AgentPromptUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    name: str
    content: str


class AgentPromptApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    apps: list[str]
    node_ids: list[str]


class AgentSkillInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str
    source: str
    apps: list[str]
    node_ids: list[str]


class AgentSkillActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str
    apps: list[str]
    node_ids: list[str]


@router.get("/providers/{app_id}/{provider_id}", response_model=dict[str, object])
def get_provider(app_id: str, provider_id: str, request: Request) -> dict[str, object]:
    require_authenticated_request(request)
    provider = _provider_store(request).get_provider(
        _safe_id(app_id), _safe_id(provider_id)
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    return public_provider(provider, include_settings=True)


@router.post("/providers/import-local", response_model=AgentImportResponse)
def import_local_providers(
    request: Request, payload: AgentClientSelectionRequest | None = None
) -> AgentImportResponse:
    require_authenticated_request(request)
    store = _provider_store(request)
    apps = _safe_apps(payload.apps if payload else [])
    if payload is not None and payload.apps and not apps:
        raise HTTPException(status_code=400, detail="no supported apps selected")
    imported_count = store.import_from_home(Path.home(), apps=apps or None)
    request.app.state.system_terminal_sink.write(
        f"Agent Provider 已从本机配置导入：{imported_count} 个\n"
    )
    return AgentImportResponse(imported_count=imported_count)


@router.post("/providers", response_model=dict[str, object])
def upsert_provider(
    payload: AgentProviderUpsertRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    app_id = _safe_id(payload.app_id)
    provider_id = _safe_id(payload.provider_id)
    if not app_id or not provider_id:
        raise HTTPException(
            status_code=400, detail="app_id and provider_id are required"
        )
    if not payload.settings_config and not payload.routing:
        raise HTTPException(status_code=400, detail="settings_config or routing is required")
    settings = dict(payload.settings_config)
    if payload.routing is not None:
        current_routing = settings.get("routing")
        merged_routing = (
            dict(current_routing) if isinstance(current_routing, dict) else {}
        )
        merged_routing.update(dict(payload.routing))
        settings["routing"] = merged_routing
    try:
        saved = _provider_store(request).upsert_provider(
            app_id=app_id,
            provider_id=provider_id,
            name=payload.name.strip() or provider_id,
            settings=settings,
            website_url=payload.website_url,
            category=payload.category,
            notes=payload.notes,
            icon=payload.icon,
            icon_color=payload.icon_color,
            is_current=payload.is_current,
            source="manual",
        )
        return public_provider(saved, include_settings=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/providers/{app_id}/{provider_id}/test", response_model=dict[str, object]
)
def test_provider_connection(
    app_id: str, provider_id: str, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    profile = _provider_store(request).runtime_profile(
        _safe_id(app_id), _safe_id(provider_id)
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="provider profile not found")

    url = _provider_models_url(profile)
    headers, params = _provider_auth(profile)
    started = perf_counter()
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers, params=params)
    except (httpx.HTTPError, OSError):
        return {
            "ok": False,
            "status_code": None,
            "latency_ms": max(0, round((perf_counter() - started) * 1000)),
            "error": "connection_failed",
        }

    latency_ms = max(0, round((perf_counter() - started) * 1000))
    result: dict[str, object] = {
        "ok": 200 <= response.status_code < 400,
        "status_code": response.status_code,
        "latency_ms": latency_ms,
    }
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        models = body.get("data") or body.get("models")
        if isinstance(models, list):
            result["model_count"] = len(models)
    return result


def _provider_models_url(profile: dict[str, object]) -> str:
    base_url = str(profile.get("base_url") or "").rstrip("/")
    api_format = str(profile.get("api_format") or "")
    if api_format == "gemini":
        if base_url.endswith("/v1beta"):
            return f"{base_url}/models"
        return f"{base_url}/v1beta/models"
    if base_url.endswith("/v1"):
        return f"{base_url}/models"
    return f"{base_url}/v1/models"


def _provider_auth(
    profile: dict[str, object],
) -> tuple[dict[str, str], dict[str, str]]:
    raw_headers = profile.get("headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    params: dict[str, str] = {}
    api_key = str(profile.get("api_key") or "")
    auth_mode = str(profile.get("auth_mode") or "none")
    if api_key and auth_mode == "bearer":
        headers.setdefault("Authorization", f"Bearer {api_key}")
    elif api_key and auth_mode == "x-api-key":
        headers.setdefault("x-api-key", api_key)
    elif api_key and auth_mode == "query":
        params["key"] = api_key
    if str(profile.get("api_format") or "") == "anthropic":
        headers.setdefault("anthropic-version", "2023-06-01")
    return headers, params


@router.post("/providers/current", response_model=dict[str, bool])
def set_current_provider(
    payload: AgentProviderCurrentRequest, request: Request
) -> dict[str, bool]:
    require_authenticated_request(request)
    updated = _provider_store(request).set_current(
        _safe_id(payload.app_id), _safe_id(payload.provider_id)
    )
    if not updated:
        raise HTTPException(status_code=404, detail="provider not found")
    return {"updated": True}


@router.delete("/providers/{app_id}/{provider_id}", response_model=dict[str, bool])
def delete_provider(app_id: str, provider_id: str, request: Request) -> dict[str, bool]:
    require_authenticated_request(request)
    deleted = _provider_store(request).delete_provider(
        _safe_id(app_id), _safe_id(provider_id)
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="provider not found")
    return {"deleted": True}


@router.post("/providers/apply", response_model=AgentQueuedResponse, status_code=202)
def queue_provider_apply(
    payload: AgentProviderApplyRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    provider = _provider_store(request).get_provider(
        _safe_id(payload.app_id), _safe_id(payload.provider_id)
    )
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")
    response = _queue_agent_shell_tasks(
        request,
        node_ids=payload.node_ids,
        action="agent_provider_apply",
        object_suffix=f"provider__{provider['app_id']}__{provider['id']}",
        build_shell=lambda windows: build_provider_apply_shell(
            provider, windows=windows, write_secrets=payload.write_secrets
        ),
    )
    return response


@router.get("/mcp/inventory", response_model=dict[str, object])
def get_mcp_inventory(request: Request) -> dict[str, object]:
    require_authenticated_request(request)
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    variants = []
    for variant in state.list_mcp_variants():
        safe = dict(variant)
        safe["spec"] = redact_sensitive(dict(variant.get("spec") or {}))
        safe.pop("spec_json", None)
        variants.append(safe)
    observations = []
    for observation in state.list_mcp_observations():
        safe = dict(observation)
        safe.pop("public_spec_json", None)
        observations.append(safe)
    return {
        "variants": variants,
        "assignments": state.list_mcp_assignments(),
        "observations": observations,
        "operations": state.list_mcp_operations(),
    }


@router.post("/mcp/scan", response_model=dict[str, object])
def scan_mcp_targets(payload: AgentScanRequest, request: Request) -> dict[str, object]:
    require_authenticated_request(request)
    apps = _safe_apps(payload.apps)
    if not payload.node_ids or not apps:
        raise HTTPException(status_code=400, detail="node_ids and apps are required")
    store = _mcp_store(request)
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    targets: list[dict[str, object]] = []
    for node_id in payload.node_ids:
        if node_id != "__local__":
            node = request.app.state.remote_node_store.get_node(node_id)
            if node is None:
                targets.append(
                    {
                        "node_id": node_id,
                        "status": "missing_node",
                        "reason": "节点不存在",
                        "mcp_ids": [],
                    }
                )
            elif node.status != "online":
                targets.append(
                    {
                        "node_id": node_id,
                        "status": "offline",
                        "reason": node.last_error or "SSH 未连接",
                        "mcp_ids": [],
                    }
                )
            elif node.os_hint == "windows" or node.username.lower() in {
                "administrator",
                "admin",
            }:
                targets.append(
                    {
                        "node_id": node_id,
                        "status": "unsupported",
                        "reason": "Windows MCP 扫描尚未支持",
                        "mcp_ids": [],
                    }
                )
            else:
                completed = request.app.state.remote_ssh_service.run_command(
                    node, build_mcp_scan_shell(apps)
                )
                if completed.returncode != 0:
                    targets.append(
                        {
                            "node_id": node_id,
                            "status": "error",
                            "reason": "远程配置读取失败",
                            "mcp_ids": [],
                        }
                    )
                else:
                    try:
                        remote_inventory = parse_mcp_scan_output(completed.stdout or "")
                    except ValueError as exc:
                        targets.append(
                            {
                                "node_id": node_id,
                                "status": "error",
                                "reason": str(exc),
                                "mcp_ids": [],
                            }
                        )
                    else:
                        discovered_remote: set[str] = set()
                        for client_id in apps:
                            observations: list[dict[str, object]] = []
                            for mcp_id, spec in remote_inventory.get(
                                client_id, {}
                            ).items():
                                discovered_remote.add(mcp_id)
                                store.upsert_server(
                                    mcp_id, spec, {client_id: True}, name=mcp_id
                                )
                                state.upsert_mcp_variant(
                                    mcp_id,
                                    client_id,
                                    node.os_hint or "linux",
                                    spec,
                                    source="scan",
                                )
                                observations.append(
                                    {
                                        "mcp_id": mcp_id,
                                        "present": True,
                                        "spec_hash": hashlib.sha256(
                                            json.dumps(
                                                spec, sort_keys=True, ensure_ascii=False
                                            ).encode()
                                        ).hexdigest(),
                                        "public_spec": redact_sensitive(spec),
                                        "status": "installed",
                                    }
                                )
                            state.replace_mcp_observations(
                                node_id, client_id, observations
                            )
                        targets.append(
                            {
                                "node_id": node_id,
                                "status": "scanned",
                                "reason": None,
                                "mcp_ids": sorted(discovered_remote, key=str.lower),
                            }
                        )
            continue
        discovered: set[str] = set()
        for client_id in apps:
            scanned = scan_mcp_home(Path.home(), client_id)
            observations: list[dict[str, object]] = []
            for mcp_id, spec in scanned.items():
                discovered.add(mcp_id)
                store.upsert_server(mcp_id, spec, {client_id: True}, name=mcp_id)
                state.upsert_mcp_variant(
                    mcp_id, client_id, "linux", spec, source="scan"
                )
                observations.append(
                    {
                        "mcp_id": mcp_id,
                        "present": True,
                        "spec_hash": hashlib.sha256(
                            json.dumps(
                                spec, sort_keys=True, ensure_ascii=False
                            ).encode()
                        ).hexdigest(),
                        "public_spec": redact_sensitive(spec),
                        "status": "installed",
                    }
                )
            state.replace_mcp_observations("__local__", client_id, observations)
        targets.append(
            {
                "node_id": "__local__",
                "status": "scanned",
                "reason": None,
                "mcp_ids": sorted(discovered, key=str.lower),
            }
        )
    return {"targets": targets}


@router.post("/mcp/import-local", response_model=AgentImportResponse)
def import_local_mcp(
    request: Request, payload: AgentClientSelectionRequest | None = None
) -> AgentImportResponse:
    require_authenticated_request(request)
    store = _mcp_store(request)
    apps = _safe_apps(payload.apps if payload else [])
    if payload is not None and payload.apps and not apps:
        raise HTTPException(status_code=400, detail="no supported apps selected")
    imported_count = store.import_from_home(Path.home(), apps=apps or None)
    request.app.state.system_terminal_sink.write(
        f"Agent MCP 已从本机配置导入：{imported_count} 个\n"
    )
    return AgentImportResponse(imported_count=imported_count)


@router.post("/mcp/servers", response_model=dict[str, object])
def upsert_mcp_server(
    payload: AgentMcpUpsertRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    server_id = _safe_id(payload.server_id)
    if not server_id:
        raise HTTPException(status_code=400, detail="server_id is required")
    if not payload.spec:
        raise HTTPException(status_code=400, detail="spec is required")
    saved = _mcp_store(request).upsert_server(
        server_id,
        dict(payload.spec),
        payload.apps,
        name=(payload.name or server_id).strip(),
        description=(payload.description or "").strip() or None,
        homepage=(payload.homepage or "").strip() or None,
        docs=(payload.docs or "").strip() or None,
        tags=[tag.strip() for tag in payload.tags if tag.strip()],
    )
    return public_mcp_server(saved)


@router.delete("/mcp/servers/{server_id}", response_model=dict[str, bool])
def delete_mcp_server(server_id: str, request: Request) -> dict[str, bool]:
    require_authenticated_request(request)
    deleted = _mcp_store(request).delete_server(server_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return {"deleted": True}


@router.post("/mcp/preview", response_model=dict[str, object])
def preview_mcp_matrix(
    payload: AgentMcpMatrixRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    store = _mcp_store(request)
    servers = store.list_servers()
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    targets: list[dict[str, object]] = []
    for assignment in payload.assignments:
        client_id = _safe_id(assignment.client_id)
        status = "ready"
        reason = None
        if client_id not in _supported_apps():
            status, reason = "unsupported", "客户端暂不支持"
        elif assignment.node_id != "__local__":
            node = request.app.state.remote_node_store.get_node(assignment.node_id)
            if node is None:
                status, reason = "missing_node", "节点不存在"
            elif node.status != "online":
                status, reason = "offline", node.last_error or "SSH 未连接"
            elif node.os_hint == "windows" or node.username.lower() in {
                "administrator",
                "admin",
            }:
                operation = "卸载" if payload.action == "uninstall" else "写入"
                status, reason = "unsupported", f"Windows MCP {operation}尚未支持"
        if status == "ready":
            if payload.action == "uninstall":
                installed_ids = {
                    item["mcp_id"]
                    for item in state.list_mcp_observations(
                        assignment.node_id, client_id
                    )
                    if item.get("present") and item.get("status") == "installed"
                }
                missing = sorted(set(assignment.mcp_ids) - installed_ids)
                if missing:
                    status, reason = (
                        "invalid",
                        f"未观测为已安装: {', '.join(missing)}",
                    )
            else:
                missing = [
                    mcp_id for mcp_id in assignment.mcp_ids if mcp_id not in servers
                ]
                if missing:
                    status, reason = "invalid", f"MCP 不存在: {', '.join(missing)}"
        targets.append(
            {
                "node_id": assignment.node_id,
                "client_id": client_id,
                "mcp_ids": assignment.mcp_ids,
                "action": payload.action,
                "status": status,
                "reason": reason,
                "preserve_existing": True,
                "backup": True,
            }
        )
    return {"targets": targets}


@router.post("/mcp/apply", response_model=AgentQueuedResponse, status_code=202)
def apply_mcp_matrix(
    payload: AgentMcpMatrixRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    store = _mcp_store(request)
    servers = store.list_servers()
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    grouped: dict[str, list[AgentMcpTargetAssignment]] = {}
    for assignment in payload.assignments:
        grouped.setdefault(assignment.node_id, []).append(assignment)
    tasks: list[str] = []
    skipped: list[str] = []
    for node_id, assignments in grouped.items():
        node = None
        if node_id != "__local__":
            node = request.app.state.remote_node_store.get_node(node_id)
            if node is None:
                skipped.append(f"{node_id}: 节点不存在")
                continue
            if node.status != "online":
                skipped.append(f"{node.name}: SSH 未连接")
                continue
            if node.os_hint == "windows" or node.username.lower() in {
                "administrator",
                "admin",
            }:
                skipped.append(f"{node.name}: Windows MCP 写入尚未支持")
                continue
        shells: list[str] = []
        persisted: dict[str, list[dict[str, object]]] = {}
        for assignment in assignments:
            client_id = _safe_id(assignment.client_id)
            if client_id not in _supported_apps():
                skipped.append(f"{node_id}/{client_id}: 客户端暂不支持")
                continue
            selected: dict[str, dict[str, object]] = {}
            platform = (node.os_hint if node is not None else "linux") or "linux"
            for mcp_id in assignment.mcp_ids:
                if mcp_id not in servers:
                    continue
                item = dict(servers[mcp_id])
                variants = state.list_mcp_variants(mcp_id, client_id)
                variant = next(
                    (row for row in variants if row["platform"] == platform), None
                )
                variant = variant or next(
                    (row for row in variants if row["platform"] == "any"), None
                )
                if variant is not None:
                    item["spec"] = variant["spec"]
                selected[mcp_id] = item
            missing = sorted(set(assignment.mcp_ids) - set(selected))
            if missing:
                skipped.append(
                    f"{node_id}/{client_id}: MCP 不存在 {', '.join(missing)}"
                )
                continue
            shell = build_mcp_apply_shell(selected, [client_id], windows=False)
            if shell:
                shells.append(shell)
                persisted[client_id] = [
                    {
                        "mcp_id": mcp_id,
                        "variant_client_id": client_id,
                        "variant_platform": "linux",
                    }
                    for mcp_id in selected
                ]
        if not shells:
            continue
        combined = "\n".join(shells)
        command = (
            ["bash", "-lc", combined]
            if node is None
            else request.app.state.remote_ssh_service.build_command(node, combined)
        )
        task = request.app.state.task_queue.enqueue(
            f"{'local' if node is None else 'remote__' + node.id}__agent__mcp_matrix",
            "agent_mcp_apply",
            plan=ActionPlan(
                commands=[command],
                command_timeout_seconds=180,
                working_dir=str(Path.cwd()),
            ),
        )
        tasks.append(task.id)
        for client_id, values in persisted.items():
            PanelStateStore(
                agent_data_root(request.app.state.config_root)
            ).replace_mcp_assignments(node_id, client_id, values)
    request.app.state.system_terminal_sink.write(
        f"Agent MCP 安装任务已入队：{len(tasks)} 个，跳过 {len(skipped)} 个\n"
    )
    return AgentQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


@router.post("/mcp/uninstall", response_model=AgentQueuedResponse, status_code=202)
def uninstall_mcp_matrix(
    payload: AgentMcpMatrixRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    grouped: dict[str, list[AgentMcpTargetAssignment]] = {}
    for assignment in payload.assignments:
        grouped.setdefault(assignment.node_id, []).append(assignment)

    tasks: list[str] = []
    skipped: list[str] = []
    for node_id, assignments in grouped.items():
        node = None
        if node_id != "__local__":
            node = request.app.state.remote_node_store.get_node(node_id)
            if node is None:
                skipped.append(f"{node_id}: 节点不存在")
                continue
            if node.status != "online":
                skipped.append(f"{node.name}: SSH 未连接")
                continue
            if node.os_hint == "windows" or node.username.lower() in {
                "administrator",
                "admin",
            }:
                skipped.append(f"{node.name}: Windows MCP 卸载尚未支持")
                continue

        shells: list[str] = []
        queued_removals: dict[str, set[str]] = {}
        for assignment in assignments:
            client_id = _safe_id(assignment.client_id)
            if client_id not in _supported_apps():
                skipped.append(f"{node_id}/{client_id}: 客户端暂不支持")
                continue
            selected_ids = {mcp_id for mcp_id in assignment.mcp_ids if mcp_id}
            installed_ids = {
                item["mcp_id"]
                for item in state.list_mcp_observations(node_id, client_id)
                if item.get("present") and item.get("status") == "installed"
            }
            missing = sorted(selected_ids - installed_ids)
            if missing:
                skipped.append(
                    f"{node_id}/{client_id}: 未观测为已安装 {', '.join(missing)}"
                )
                continue
            shell = build_mcp_remove_shell(selected_ids, [client_id], windows=False)
            if shell:
                shells.append(shell)
                queued_removals.setdefault(client_id, set()).update(selected_ids)

        if not shells:
            continue
        combined = "\n".join(shells)
        command = (
            ["bash", "-lc", combined]
            if node is None
            else request.app.state.remote_ssh_service.build_command(node, combined)
        )
        task = request.app.state.task_queue.enqueue(
            f"{'local' if node is None else 'remote__' + node.id}__agent__mcp_uninstall",
            "agent_mcp_uninstall",
            plan=ActionPlan(
                commands=[command],
                command_timeout_seconds=180,
                working_dir=str(Path.cwd()),
            ),
        )
        tasks.append(task.id)
        for client_id, mcp_ids in queued_removals.items():
            state.remove_mcp_assignments(node_id, client_id, mcp_ids)
            state.record_mcp_operation(
                f"{task.id}:{client_id}:uninstall",
                node_id,
                client_id,
                "uninstall",
                "queued",
                task_id=task.id,
            )

    request.app.state.system_terminal_sink.write(
        f"Agent MCP 卸载任务已入队：{len(tasks)} 个，跳过 {len(skipped)} 个\n"
    )
    return AgentQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


@router.post("/mcp/sync", response_model=AgentQueuedResponse, status_code=202)
def queue_mcp_sync(
    payload: AgentMcpSyncRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    if not payload.server_ids:
        raise HTTPException(status_code=400, detail="server_ids is required")
    if not payload.apps:
        raise HTTPException(status_code=400, detail="apps is required")
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")
    invalid_apps = sorted(set(payload.apps) - _supported_apps())
    if invalid_apps:
        raise HTTPException(
            status_code=400, detail=f"unsupported apps: {', '.join(invalid_apps)}"
        )
    selected = _mcp_store(request).selected_servers(payload.server_ids)
    if not selected:
        raise HTTPException(status_code=404, detail="selected MCP servers not found")
    missing_servers = sorted(set(payload.server_ids) - set(selected))
    if missing_servers:
        raise HTTPException(
            status_code=404,
            detail=f"MCP servers not found: {', '.join(missing_servers)}",
        )
    return _queue_agent_shell_tasks(
        request,
        node_ids=payload.node_ids,
        action="agent_mcp_sync",
        object_suffix="mcp",
        build_shell=lambda windows: build_mcp_apply_shell(
            selected, payload.apps, windows=windows
        ),
    )


@router.post("/prompts", response_model=dict[str, object])
def upsert_prompt(
    payload: AgentPromptUpsertRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    prompt_id = _safe_id(payload.prompt_id)
    if not prompt_id:
        raise HTTPException(status_code=400, detail="prompt_id is required")
    if not payload.content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    return _prompt_store(request).upsert_prompt(
        prompt_id, payload.name.strip() or prompt_id, payload.content
    )


@router.delete("/prompts/{prompt_id}", response_model=dict[str, bool])
def delete_prompt(prompt_id: str, request: Request) -> dict[str, bool]:
    require_authenticated_request(request)
    deleted = _prompt_store(request).delete_prompt(prompt_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="prompt not found")
    return {"deleted": True}


@router.post("/prompts/apply", response_model=AgentQueuedResponse, status_code=202)
def queue_prompt_apply(
    payload: AgentPromptApplyRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    prompt = _prompt_store(request).get_prompt(payload.prompt_id)
    if prompt is None:
        raise HTTPException(status_code=404, detail="prompt not found")
    if not payload.apps:
        raise HTTPException(status_code=400, detail="apps is required")
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")

    tasks: list[str] = []
    skipped: list[str] = []
    for node_id in payload.node_ids:
        for app_id in payload.apps:
            windows = False
            command_prefix = None
            node_name = "当前 WSL"
            if node_id != "__local__":
                node = request.app.state.remote_node_store.get_node(node_id)
                if node is None:
                    skipped.append(f"{node_id}: 节点不存在")
                    continue
                node_name = node.name
                if node.status != "online":
                    skipped.append(f"{node.name}: SSH 未连接")
                    continue
                windows = node.os_hint == "windows" or node.username.lower() in {
                    "administrator",
                    "admin",
                }
                command_prefix = node
            shell = build_prompt_apply_shell(
                app_id, str(prompt.get("content") or ""), windows=windows
            )
            if not shell:
                skipped.append(f"{node_name}: {app_id} 暂不支持写入")
                continue
            command = (
                ["bash", "-lc", shell]
                if command_prefix is None
                else request.app.state.remote_ssh_service.build_command(
                    command_prefix, shell
                )
            )
            task = request.app.state.task_queue.enqueue(
                f"{'local' if node_id == '__local__' else 'remote'}__agent__prompt__{app_id}",
                "agent_prompt_apply",
                plan=ActionPlan(
                    commands=[command],
                    command_timeout_seconds=90,
                    working_dir=str(Path.cwd()),
                ),
            )
            tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"Agent Prompt 写入任务已入队：{len(tasks)} 个，跳过 {len(skipped)} 个\n"
    )
    return AgentQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


@router.post("/skills/install", response_model=AgentQueuedResponse, status_code=202)
def queue_skill_install(
    payload: AgentSkillInstallRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    skill_name = safe_skill_name(payload.skill_name)
    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name is required")
    if not payload.source.strip():
        raise HTTPException(status_code=400, detail="source is required")
    return _queue_agent_app_tasks(
        request,
        node_ids=payload.node_ids,
        apps=payload.apps,
        action="agent_skill_install",
        object_suffix=f"skill__{skill_name}",
        build_shell=lambda app_id, windows: build_skill_install_shell(
            app_id, skill_name, payload.source, windows=windows
        ),
    )


@router.post("/skills/update", response_model=AgentQueuedResponse, status_code=202)
def queue_skill_update(
    payload: AgentSkillActionRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    skill_name = safe_skill_name(payload.skill_name)
    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name is required")
    return _queue_agent_app_tasks(
        request,
        node_ids=payload.node_ids,
        apps=payload.apps,
        action="agent_skill_update",
        object_suffix=f"skill__{skill_name}",
        build_shell=lambda app_id, windows: build_skill_update_shell(
            app_id, skill_name, windows=windows
        ),
    )


@router.post("/skills/delete", response_model=AgentQueuedResponse, status_code=202)
def queue_skill_delete(
    payload: AgentSkillActionRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    skill_name = safe_skill_name(payload.skill_name)
    if not skill_name:
        raise HTTPException(status_code=400, detail="skill_name is required")
    return _queue_agent_app_tasks(
        request,
        node_ids=payload.node_ids,
        apps=payload.apps,
        action="agent_skill_delete",
        object_suffix=f"skill__{skill_name}",
        build_shell=lambda app_id, windows: build_skill_delete_shell(
            app_id, skill_name, windows=windows
        ),
    )


def _queue_agent_app_tasks(
    request: Request,
    *,
    node_ids: list[str],
    apps: list[str],
    action: str,
    object_suffix: str,
    build_shell,
) -> AgentQueuedResponse:
    if not apps:
        raise HTTPException(status_code=400, detail="apps is required")
    if not node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")
    tasks: list[str] = []
    skipped: list[str] = []
    for node_id in node_ids:
        for app_id in apps:
            windows = False
            command_prefix = None
            node_name = "当前 WSL"
            object_id = f"local__agent__{object_suffix}__{app_id}"
            if node_id != "__local__":
                node = request.app.state.remote_node_store.get_node(node_id)
                if node is None:
                    skipped.append(f"{node_id}: 节点不存在")
                    continue
                node_name = node.name
                if node.status != "online":
                    skipped.append(f"{node.name}: SSH 未连接")
                    continue
                windows = node.os_hint == "windows" or node.username.lower() in {
                    "administrator",
                    "admin",
                }
                command_prefix = node
                object_id = f"remote__{node.id}__agent__{object_suffix}__{app_id}"
            shell = build_shell(app_id, windows)
            if not shell:
                skipped.append(f"{node_name}: {app_id} 暂不支持该 Skill 操作")
                continue
            command = (
                ["bash", "-lc", shell]
                if command_prefix is None
                else request.app.state.remote_ssh_service.build_command(
                    command_prefix, shell
                )
            )
            task = request.app.state.task_queue.enqueue(
                object_id,
                action,
                plan=ActionPlan(
                    commands=[command],
                    command_timeout_seconds=180,
                    working_dir=str(Path.cwd()),
                ),
            )
            tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"Agent Skill 任务已入队：{action} · {len(tasks)} 个，跳过 {len(skipped)} 个\n"
    )
    return AgentQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


def _queue_agent_shell_tasks(
    request: Request,
    *,
    node_ids: list[str],
    action: str,
    object_suffix: str,
    build_shell,
) -> AgentQueuedResponse:
    tasks: list[str] = []
    skipped: list[str] = []
    for node_id in node_ids:
        windows = False
        command_prefix = None
        node_name = "当前 WSL"
        object_id = f"local__agent__{object_suffix}"
        if node_id != "__local__":
            node = request.app.state.remote_node_store.get_node(node_id)
            if node is None:
                skipped.append(f"{node_id}: 节点不存在")
                continue
            node_name = node.name
            if node.status != "online":
                skipped.append(f"{node.name}: SSH 未连接")
                continue
            windows = node.os_hint == "windows" or node.username.lower() in {
                "administrator",
                "admin",
            }
            command_prefix = node
            object_id = f"remote__{node.id}__agent__{object_suffix}"
        shell = build_shell(windows)
        if not shell:
            skipped.append(f"{node_name}: 暂不支持该 Agent 写入")
            continue
        command = (
            ["bash", "-lc", shell]
            if command_prefix is None
            else request.app.state.remote_ssh_service.build_command(
                command_prefix, shell
            )
        )
        task: TaskRecord = request.app.state.task_queue.enqueue(
            object_id,
            action,
            plan=ActionPlan(
                commands=[command],
                command_timeout_seconds=120,
                working_dir=str(Path.cwd()),
            ),
        )
        tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"Agent 任务已入队：{action} · {len(tasks)} 个，跳过 {len(skipped)} 个\n"
    )
    return AgentQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


def _mcp_store(request: Request) -> AgentMcpStore:
    return AgentMcpStore(agent_data_root(request.app.state.config_root))


def _prompt_store(request: Request) -> AgentPromptStore:
    return AgentPromptStore(agent_data_root(request.app.state.config_root))


def _provider_store(request: Request) -> AgentProviderStore:
    return AgentProviderStore(agent_data_root(request.app.state.config_root))


def _safe_id(value: str) -> str:
    return "".join(
        ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip()
    ).strip("-")


def _safe_apps(values: list[str]) -> list[str]:
    supported = _supported_apps()
    return [app_id for value in values if (app_id := _safe_id(value)) in supported]


def _supported_apps() -> set[str]:
    from app.services.agent_clients import AGENT_CLIENTS

    return {client.id for client in AGENT_CLIENTS}
