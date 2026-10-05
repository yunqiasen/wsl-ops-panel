from __future__ import annotations

import hashlib
import subprocess
from time import perf_counter
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import ActionPlan
from app.services.agent_provider_projection import ProviderProjection, provider_projection
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
from app.services.agent_mcp_adapters import (
    CLIENT_PATHS,
    apply_mcp_to_home,
    remove_mcp_from_home,
)
from app.services.agent_profiles import AgentProfileStore
from app.services.agent_prompts import (
    AgentPromptStore,
    PromptFileConflictError,
    build_prompt_apply_shell,
)
from app.services.agent_provider_adapters import (
    ADDITIVE_PROVIDER_APPS,
    build_native_settings,
)
from app.services.agent_providers import (
    AgentProviderStore,
    CurrentProviderError,
    build_provider_apply_shell,
    build_provider_remove_shell,
    public_provider,
    redact_sensitive,
)
from app.services.agent_reconciler import (
    AgentReconciler,
    AgentResourceOperation,
    AgentResourcePlan,
)
from app.services.agent_router_config import AgentRouterConfigError, AgentRouterConfigStore
from app.services.agent_skill_store import AgentSkillStore, SkillAssignedError
from app.services.agent_workbench import build_agent_workbench_context
from app.services.state_store import PanelStateStore
from app.services.agent_skills import (
    build_skill_delete_shell,
    build_skill_install_shell,
    build_skill_update_shell,
    safe_skill_name,
    scan_agent_skills,
    uninstall_skill_from_home,
    update_skill_to_home,
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
    form: dict[str, object] | None = None
    meta: dict[str, object] | None = None
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


class AgentProviderActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    write_secrets: bool = True


class AgentProviderDuplicateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_id: str | None = None
    new_name: str | None = None


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


class AgentMcpLocalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    mcp_ids: list[str] = Field(default_factory=list)


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
    description: str | None = None
    content: str


class AgentPromptApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    apps: list[str]
    node_ids: list[str]


class AgentPromptClientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str


class AgentPromptLocalApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    prompt_id: str | None = None
    content: str | None = None


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


class AgentSkillLocalInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    skill_name: str
    source: str
    mode: Literal["copy", "symlink"] = "copy"


class AgentSkillLocalActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    skill_name: str
    source: str | None = None
    mode: Literal["copy", "symlink"] | None = None


class AgentMcpImportCurrentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    mcp_ids: list[str] = Field(default_factory=list)


class AgentSkillImportSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    name: str
    source: str
    description: str | None = None
    version: str | None = None


class AgentSkillImportCurrentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    skill_name: str
    skill_id: str | None = None
    name: str | None = None


class AgentPromptImportCurrentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    prompt_id: str | None = None
    name: str | None = None


class AgentResourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["install", "update", "uninstall", "sync"] = "sync"
    resource_type: Literal["provider", "mcp", "skill", "prompt", "router"] | None = None
    resource_ids: list[str] = Field(default_factory=list)
    client_id: str | None = None
    node_id: str = "__local__"
    profile_id: str | None = None


class AgentResourceOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: Literal["provider", "mcp", "skill", "prompt", "profile"]
    resource_ids: list[str] = Field(min_length=1)
    client_id: str | None = None


class AgentProfileItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str
    resource_type: Literal["provider", "mcp", "skill", "prompt", "router"]
    resource_id: str
    config: dict[str, object] = Field(default_factory=dict)
    sort_index: int = 0


class AgentProfileUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str | None = None
    items: list[AgentProfileItemRequest] = Field(default_factory=list)


class AgentProfileApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = "__local__"
    client_id: str | None = None


@router.get("/library", response_model=dict[str, object])
def get_agent_library(request: Request) -> dict[str, object]:
    require_authenticated_request(request)
    context = build_agent_workbench_context(
        request.app.state.config_root, home=Path.home()
    )
    return dict(context["agent_library"])


@router.post("/mcp/import-current", response_model=dict[str, object])
def import_current_mcp(
    payload: AgentMcpImportCurrentRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "mcp")
    selected_ids = {item.strip() for item in payload.mcp_ids if item.strip()}
    imported = _mcp_store(request).import_from_home(
        Path.home(),
        apps=[client_id],
        node_id="__local__",
        platform="linux",
        server_ids=selected_ids or None,
    )
    return {"client_id": client_id, "imported_count": imported}


@router.post("/skills/import-source", response_model=dict[str, object])
def import_skill_source(
    payload: AgentSkillImportSourceRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    try:
        return _skill_store(request).import_source(
            payload.skill_id,
            payload.name,
            payload.source,
            description=payload.description,
            version=payload.version,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/skills/import-current", response_model=dict[str, object])
def import_current_skill(
    payload: AgentSkillImportCurrentRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "skills")
    try:
        return _skill_store(request).import_from_client(
            Path.home(),
            client_id,
            payload.skill_name,
            skill_id=payload.skill_id,
            name=payload.name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/resources/plan", response_model=dict[str, object])
def plan_agent_resources(
    payload: AgentResourceRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    return _build_resource_plan(request, payload).as_dict()


@router.post("/resources/reconcile", response_model=dict[str, object])
def reconcile_agent_resources(
    payload: AgentResourceRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    return _reconcile_resource_request(request, payload)


@router.put("/resources/order", response_model=dict[str, object])
def reorder_agent_resources(
    payload: AgentResourceOrderRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _safe_id(payload.client_id) if payload.client_id else None
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    try:
        resource_ids = state.reorder_agent_resources(
            payload.resource_type,
            payload.resource_ids,
            client_id=client_id,
        )
    except ValueError as exc:
        status_code = 409 if "完整" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    return {
        "resource_type": payload.resource_type,
        "client_id": client_id,
        "resource_ids": resource_ids,
    }


@router.delete(
    "/resources/{resource_type}/{resource_id}", response_model=dict[str, object]
)
def delete_agent_resource(
    resource_type: str, resource_id: str, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    clean_type = resource_type.strip().lower()
    clean_id = resource_id.strip()
    if clean_type not in {"mcp", "skill", "prompt"} or not clean_id:
        raise HTTPException(status_code=400, detail="resource type or id is invalid")
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    assignments = _resource_assignments(state, clean_type, clean_id)
    remote = [item for item in assignments if item["node_id"] != "__local__"]
    if remote:
        targets = ", ".join(
            f"{item['node_id']}/{item['client_id']}" for item in remote
        )
        raise HTTPException(
            status_code=409,
            detail=f"资源仍分配给非本地目标: {targets}",
        )

    uninstalled: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    for assignment in assignments:
        operation = AgentResourceOperation(
            action="uninstall",
            resource_type=clean_type,  # type: ignore[arg-type]
            resource_id=clean_id,
            client_id=str(assignment["client_id"]),
            node_id="__local__",
            reason="delete_from_library",
        )
        if not _resource_observed_present(
            state, clean_type, clean_id, "__local__", operation.client_id
        ):
            _remove_resource_assignment(
                state, clean_type, clean_id, "__local__", operation.client_id
            )
            uninstalled.append(
                {
                    "client_id": operation.client_id,
                    "verified": True,
                    "already_missing": True,
                }
            )
            continue
        try:
            result = _execute_resource_operation(request, operation)
        except Exception as exc:  # converted to a transactional 409 below
            failures.append(
                {"client_id": operation.client_id, "error": _operation_error(exc)}
            )
        else:
            if bool(result.get("verified")):
                uninstalled.append(result)
            else:
                failures.append(
                    {
                        "client_id": operation.client_id,
                        "error": "客户端回读未通过",
                    }
                )
    if failures:
        raise HTTPException(
            status_code=409,
            detail={"message": "资源卸载未全部完成", "failures": failures},
        )

    try:
        if clean_type == "mcp":
            deleted = _mcp_store(request).delete_server(clean_id)
        elif clean_type == "skill":
            deleted = _skill_store(request).delete(clean_id)
        else:
            deleted = _prompt_store(request).delete_prompt(clean_id)
    except SkillAssignedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="resource not found")
    return {"deleted": True, "resource_type": clean_type, "uninstalled": uninstalled}


@router.get("/profiles", response_model=dict[str, object])
def list_agent_profiles(request: Request) -> dict[str, object]:
    require_authenticated_request(request)
    return {
        "profiles": [
            _public_profile(profile) for profile in _profile_store(request).list()
        ]
    }


@router.put("/profiles/{profile_id}", response_model=dict[str, object])
def upsert_agent_profile(
    profile_id: str, payload: AgentProfileUpsertRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    try:
        return _profile_store(request).upsert(
            profile_id,
            payload.name,
            description=payload.description,
            items=[item.model_dump() for item in payload.items],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/profiles/{profile_id}/apply", response_model=dict[str, object])
def apply_agent_profile(
    profile_id: str, payload: AgentProfileApplyRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    return _reconcile_resource_request(
        request,
        AgentResourceRequest(
            action="sync",
            node_id=payload.node_id,
            client_id=payload.client_id,
            profile_id=profile_id,
        ),
    )


@router.delete("/profiles/{profile_id}", response_model=dict[str, bool])
def delete_agent_profile(profile_id: str, request: Request) -> dict[str, bool]:
    require_authenticated_request(request)
    try:
        deleted = _profile_store(request).delete(profile_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="profile not found")
    return {"deleted": True}


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
    apps = _safe_apps(payload.apps if payload else [], feature="providers")
    if payload is not None and payload.apps and not apps:
        raise HTTPException(status_code=400, detail="no supported apps selected")
    import_apps = apps or sorted(_supported_apps("providers"))
    active = _active_takeover_apps(request, import_apps)
    if active:
        raise HTTPException(
            status_code=409,
            detail=_takeover_conflict_message(active),
        )
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
    if not payload.settings_config and not payload.routing and payload.form is None:
        raise HTTPException(
            status_code=400, detail="settings_config, routing or form is required"
        )
    store = _provider_store(request)
    existing = store.get_provider(app_id, provider_id)
    settings = dict(payload.settings_config)
    meta = dict(existing.get("meta") or {}) if existing is not None else {}
    if payload.meta is not None:
        meta.update(dict(payload.meta))
    if payload.form is not None:
        form = {
            **dict(payload.form),
            "provider_id": provider_id,
            "name": payload.name.strip() or provider_id,
        }
        settings = build_native_settings(
            app_id,
            form,
            dict(existing.get("settings_config") or {}) if existing else settings,
        )
        for key in (
            "api_format",
            "auth_mode",
            "full_url",
            "use_outbound_proxy",
            "model_map",
            "headers",
        ):
            if key in form:
                meta[key] = form[key]
    if payload.routing is not None:
        current_routing = settings.get("routing")
        merged_routing = (
            dict(current_routing) if isinstance(current_routing, dict) else {}
        )
        merged_routing.update(dict(payload.routing))
        settings["routing"] = merged_routing
    is_current = (
        payload.is_current
        if "is_current" in payload.model_fields_set
        else bool(existing.get("is_current"))
        if existing is not None
        else False
    )
    try:
        saved = store.upsert_provider(
            app_id=app_id,
            provider_id=provider_id,
            name=payload.name.strip() or provider_id,
            settings=settings,
            meta=meta,
            website_url=payload.website_url,
            category=payload.category,
            notes=payload.notes,
            icon=payload.icon,
            icon_color=payload.icon_color,
            is_current=is_current,
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

    # CC Switch 的连通性检查只探测 Base URL：收到任意 HTTP 响应就说明
    # 网关可达，401/404 等业务状态不等同于网络断开。
    url = _provider_probe_url(profile)
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
        "ok": True,
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


def _provider_probe_url(profile: dict[str, object]) -> str:
    """Return the configured base endpoint used by the reachability probe.

    The probe deliberately does not append ``/models`` or a protocol-specific
    generation path.  A provider may expose only a custom gateway route, and
    an HTTP error response still proves that DNS, TCP and TLS reached the
    upstream.
    """
    return str(profile.get("base_url") or "").strip()


def _provider_models_url(profile: dict[str, object]) -> str:
    """Compatibility alias for callers that used the old helper name."""
    return _provider_probe_url(profile)


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


@router.post(
    "/providers/{app_id}/{provider_id}/duplicate",
    response_model=dict[str, object],
)
def duplicate_provider(
    app_id: str,
    provider_id: str,
    request: Request,
    payload: AgentProviderDuplicateRequest | None = None,
) -> dict[str, object]:
    require_authenticated_request(request)
    store = _provider_store(request)
    try:
        duplicated = store.duplicate_provider(
            _safe_id(app_id),
            _safe_id(provider_id),
            new_id=_safe_id(payload.new_id) if payload and payload.new_id else None,
            new_name=payload.new_name.strip() if payload and payload.new_name else None,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="provider not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return public_provider(duplicated, include_settings=True)


@router.post(
    "/providers/{app_id}/{provider_id}/activate",
    response_model=dict[str, object],
)
def activate_provider(
    app_id: str,
    provider_id: str,
    request: Request,
    response: Response,
    payload: AgentProviderActivateRequest | None = None,
) -> dict[str, object]:
    require_authenticated_request(request)
    clean_app = _safe_id(app_id)
    clean_provider = _safe_id(provider_id)
    store = _provider_store(request)
    provider = store.get_provider(clean_app, clean_provider)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if bool(provider.get("meta", {}).get("native_read_only")):
        raise HTTPException(status_code=409, detail="原生只读 Provider 只用于查看")

    if clean_app in _active_takeover_apps(request, [clean_app]):
        runtime = store.runtime_profile(clean_app, clean_provider)
        if runtime is None:
            raise HTTPException(status_code=400, detail="Provider 缺少可用端点")
        router_store = AgentRouterConfigStore(
            agent_data_root(request.app.state.config_root)
        )
        current = router_store.get_provider(clean_app) or {}
        for key in (
            "auto_failover",
            "max_retries",
            "failure_threshold",
            "cooldown_seconds",
            "fallbacks",
        ):
            if key in current:
                runtime[key] = current[key]
        saved = router_store.set_provider(
            clean_app, runtime, provider_id=clean_provider
        )
        store.set_current(clean_app, clean_provider)
        return {
            "mode": "router",
            "queued_count": 0,
            "tasks": [],
            "provider_id": clean_provider,
            "provider": redact_sensitive(saved),
        }

    apply_provider = store.provider_for_apply(
        clean_app, clean_provider, include_secrets=True
    )
    if apply_provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    queued = _queue_agent_shell_tasks(
        request,
        node_ids=["__local__"],
        projection=_provider_projection(request, apply_provider, write_secrets=(payload.write_secrets if payload else True)),
        action="agent_provider_add"
        if clean_app in ADDITIVE_PROVIDER_APPS
        else "agent_provider_activate",
        object_suffix=f"provider__{clean_app}__{clean_provider}",
        build_shell=lambda windows: build_provider_apply_shell(
            apply_provider,
            windows=windows,
            write_secrets=(payload.write_secrets if payload else True),
        ),
    )
    if queued.queued_count < 1:
        raise HTTPException(status_code=400, detail=queued.skipped or "Provider 写入未入队")
    response.status_code = 202
    return {
        **queued.model_dump(),
        "mode": "additive"
        if clean_app in ADDITIVE_PROVIDER_APPS
        else "direct",
        "provider_id": clean_provider,
    }


@router.post(
    "/providers/{app_id}/{provider_id}/remove-live",
    response_model=dict[str, object],
)
def remove_live_provider(
    app_id: str,
    provider_id: str,
    request: Request,
    response: Response,
) -> dict[str, object]:
    require_authenticated_request(request)
    clean_app = _safe_id(app_id)
    clean_provider = _safe_id(provider_id)
    if clean_app not in ADDITIVE_PROVIDER_APPS:
        raise HTTPException(status_code=400, detail="当前客户端使用切换模式")
    store = _provider_store(request)
    provider = store.get_provider(clean_app, clean_provider)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if bool(provider.get("meta", {}).get("native_read_only")):
        raise HTTPException(status_code=409, detail="原生只读 Provider 由客户端维护")
    queued = _queue_agent_shell_tasks(
        request,
        node_ids=["__local__"],
        action="agent_provider_remove",
        projection=_provider_projection(request, provider, operation="remove"),
        object_suffix=f"provider_remove__{clean_app}__{clean_provider}",
        build_shell=lambda windows: build_provider_remove_shell(
            clean_app, clean_provider, windows=windows
        ),
    )
    if queued.queued_count < 1:
        raise HTTPException(status_code=400, detail=queued.skipped or "Provider 移除未入队")
    response.status_code = 202
    return {
        **queued.model_dump(),
        "mode": "remove",
        "provider_id": clean_provider,
    }


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
    clean_app = _safe_id(app_id)
    clean_provider = _safe_id(provider_id)
    provider_store = _provider_store(request)
    provider = provider_store.get_provider(clean_app, clean_provider)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    if bool(provider.get("meta", {}).get("native_read_only")):
        raise HTTPException(status_code=409, detail="原生只读 Provider 由客户端维护")
    if clean_app in ADDITIVE_PROVIDER_APPS:
        try:
            live_ids = provider_store.live_provider_ids(clean_app, Path.home())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if clean_provider in live_ids:
            raise HTTPException(
                status_code=409, detail="先从客户端移除 Provider，再从数据库删除"
            )
    router_store = AgentRouterConfigStore(agent_data_root(request.app.state.config_root))
    if bool(provider.get("is_current")) or (
        router_store.snapshot().get("provider_ids", {}).get(clean_app) == clean_provider
    ):
        raise HTTPException(status_code=409, detail="当前 Provider 不能删除，请先切换当前项")
    try:
        deleted = provider_store.delete_provider(clean_app, clean_provider)
        router_store.remove_provider_reference(clean_app, clean_provider)
    except CurrentProviderError as exc:
        raise HTTPException(status_code=409, detail="当前 Provider 不能删除，请先切换当前项") from exc
    except AgentRouterConfigError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="provider not found")
    return {"deleted": True}


@router.post("/providers/apply", response_model=AgentQueuedResponse, status_code=202)
def queue_provider_apply(
    payload: AgentProviderApplyRequest, request: Request
) -> AgentQueuedResponse:
    require_authenticated_request(request)
    clean_app = _safe_id(payload.app_id)
    clean_provider = _safe_id(payload.provider_id)
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")
    if "__local__" in payload.node_ids:
        active = _active_takeover_apps(request, [clean_app])
        if active:
            raise HTTPException(
                status_code=409,
                detail=_takeover_conflict_message(active),
            )
    provider = _provider_store(request).get_provider(clean_app, clean_provider)
    if provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    apply_provider = _provider_store(request).provider_for_apply(
        str(provider["app_id"]), str(provider["id"]), include_secrets=payload.write_secrets
    )
    if apply_provider is None:
        raise HTTPException(status_code=404, detail="provider not found")
    response = _queue_agent_shell_tasks(
        request,
        node_ids=payload.node_ids,
        action="agent_provider_apply",
        projection=_provider_projection(request, apply_provider, write_secrets=payload.write_secrets),
        object_suffix=f"provider__{provider['app_id']}__{provider['id']}",
        build_shell=lambda windows: build_provider_apply_shell(
            apply_provider, windows=windows, write_secrets=payload.write_secrets
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
    apps = _safe_apps(payload.apps, feature="mcp")
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
                                observations.append(
                                    _mcp_observation(
                                        store,
                                        client_id,
                                        node.os_hint or "linux",
                                        mcp_id,
                                        spec,
                                    )
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
        errors = []
        for client_id in apps:
            try:
                observations = store.refresh_observations(Path.home(), client_id)
                discovered.update(str(item['mcp_id']) for item in observations)
            except ValueError as exc:
                errors.append(str(exc))
        targets.append({
            "node_id": "__local__", "status": "error" if errors else "scanned",
            "reason": "; ".join(errors) if errors else None,
            "mcp_ids": sorted(discovered, key=str.lower),
        })
    return {"targets": targets}


@router.post("/mcp/local/install", response_model=dict[str, object])
def install_local_mcp(
    payload: AgentMcpLocalRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    return _run_local_mcp_operation(request, payload, action="install")


@router.post("/mcp/local/uninstall", response_model=dict[str, object])
def uninstall_local_mcp(
    payload: AgentMcpLocalRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    return _run_local_mcp_operation(request, payload, action="uninstall")


def _run_local_mcp_operation(
    request: Request, payload: AgentMcpLocalRequest, *, action: Literal["install", "uninstall"]
) -> dict[str, object]:
    client_id = _safe_id(payload.client_id)
    ids = list(dict.fromkeys(_safe_id(value) for value in payload.mcp_ids if _safe_id(value)))
    if not client_id or client_id not in CLIENT_PATHS:
        raise HTTPException(status_code=400, detail="client_id 暂不支持本地 MCP 写入")
    from app.services.agent_clients import get_agent_client

    client = get_agent_client(client_id)
    if client is None or "mcp" not in client.write_support:
        raise HTTPException(status_code=400, detail=f"{client_id} 暂不支持本地 MCP 写入")
    if not ids:
        raise HTTPException(status_code=400, detail="mcp_ids is required")

    store = _mcp_store(request)
    servers = store.list_servers()
    home = Path.home()
    try:
        before = store.scan_home(home, client_id)
    except ValueError as exc:
        error = str(exc)
        raise HTTPException(status_code=400, detail=error) from None

    if action == "install":
        missing = sorted(set(ids) - set(servers))
        if missing:
            raise HTTPException(status_code=404, detail=f"MCP 不存在: {', '.join(missing)}")
        resolved = {
            server_id: store.resolve_server_for_client(server_id, client_id, "linux")
            for server_id in ids
        }
        selected = {
            server_id: dict((resolved[server_id] or {}).get("spec") or {})
            for server_id in ids
        }
        try:
            apply_mcp_to_home(home, client_id, selected)
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"安装 MCP 失败: {exc}") from exc
    else:
        missing = sorted(set(ids) - set(before))
        if missing:
            raise HTTPException(
                status_code=409,
                detail=f"当前客户端未观测为已安装: {', '.join(missing)}",
            )
        try:
            remove_mcp_from_home(home, client_id, set(ids))
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=f"卸载 MCP 失败: {exc}") from exc

    try:
        after = store.scan_home(home, client_id)
    except ValueError as exc:
        error = str(exc)
        raise HTTPException(status_code=502, detail=error) from None

    verified = all(
        (server_id in after) if action == "install" else (server_id not in after)
        for server_id in ids
    )
    if action == "install":
        added = sorted(set(ids) - set(before), key=str.lower)
        updated = sorted(
            server_id
            for server_id in ids
            if server_id in before and before.get(server_id) != after.get(server_id)
        )
        removed: list[str] = []
    else:
        added = []
        updated = []
        removed = sorted(set(ids) - set(after), key=str.lower)

    _persist_local_mcp_observations(request, client_id, after)
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    if verified and action == "install":
        for server_id in ids:
            resolved_server = resolved[server_id]
            if resolved_server is None:
                continue
            state.set_mcp_assignment(
                "__local__",
                client_id,
                server_id,
                variant_client_id=str(
                    resolved_server.get("variant_client_id") or client_id
                ),
                variant_platform=str(
                    resolved_server.get("variant_platform") or "any"
                ),
            )
    elif verified:
        state.remove_mcp_assignments("__local__", client_id, set(ids))
    operation_id = f"local:{client_id}:{action}:{hashlib.sha256(','.join(ids).encode()).hexdigest()[:16]}"
    state.record_mcp_operation(
        operation_id, "__local__", client_id, action, "verified" if verified else "failed",
        error=None if verified else "本地配置回读未通过",
    )
    return {
        "client_id": client_id,
        "added": added,
        "updated": updated,
        "removed": removed,
        "verified": verified,
        "path": str(home / CLIENT_PATHS[client_id]),
    }


def _persist_local_mcp_observations(
    request: Request, client_id: str, scanned: dict[str, dict[str, object]]
) -> None:
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    store = _mcp_store(request)
    observations: list[dict[str, object]] = []
    for mcp_id, spec in scanned.items():
        observations.append(
            _mcp_observation(store, client_id, "linux", mcp_id, spec)
        )
    state.replace_mcp_observations("__local__", client_id, observations)


def _mcp_observation(
    store: AgentMcpStore,
    client_id: str,
    platform: str,
    mcp_id: str,
    spec: dict[str, object],
) -> dict[str, object]:
    return store.observation_for(client_id, platform, mcp_id, spec)


@router.post("/mcp/import-local", response_model=AgentImportResponse)
def import_local_mcp(
    request: Request, payload: AgentClientSelectionRequest | None = None
) -> AgentImportResponse:
    require_authenticated_request(request)
    store = _mcp_store(request)
    apps = _safe_apps(payload.apps if payload else [], feature="mcp")
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
        description=(
            payload.description.strip() if payload.description is not None else None
        ),
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
        if client_id not in _supported_apps("mcp"):
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
            if client_id not in _supported_apps("mcp"):
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
            if client_id not in _supported_apps("mcp"):
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
    invalid_apps = sorted(set(payload.apps) - _supported_apps("mcp"))
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


@router.post("/prompts/import-current", response_model=dict[str, object])
def import_current_prompt(
    payload: AgentPromptImportCurrentRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "prompts")
    try:
        return _prompt_store(request).import_current(
            Path.home(),
            client_id,
            prompt_id=payload.prompt_id,
            name=payload.name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/prompts/local/apply", response_model=dict[str, object])
def apply_local_prompt(
    payload: AgentPromptLocalApplyRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "prompts")
    store = _prompt_store(request)
    prompt_id = _safe_id(payload.prompt_id) if payload.prompt_id else ""
    if prompt_id:
        if store.get_prompt(prompt_id) is None:
            raise HTTPException(status_code=404, detail="prompt not found")
    elif payload.content is not None:
        prompt_id = f"{client_id}-adhoc"
        store.upsert_prompt(prompt_id, f"{client_id} 临时提示词", payload.content)
    else:
        raise HTTPException(status_code=400, detail="prompt_id or content is required")
    try:
        result = store.install_local(Path.home(), client_id, prompt_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.system_terminal_sink.write(
        f"Agent Prompt 本地应用完成：{result['client_id']}\n"
    )
    return result


@router.post("/prompts/local/restore", response_model=dict[str, object])
def restore_local_prompt(
    payload: AgentPromptClientRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "prompts")
    try:
        result = _prompt_store(request).restore_local(Path.home(), client_id)
    except PromptFileConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.system_terminal_sink.write(
        f"Agent Prompt 本地恢复完成：{result['client_id']}\n"
    )
    return result


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
        prompt_id,
        payload.name.strip() or prompt_id,
        payload.content,
        description=(payload.description or "").strip() or None,
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


@router.post("/skills/local/install", response_model=dict[str, object])
def install_local_skill(
    payload: AgentSkillLocalInstallRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "skills")
    skill_id = safe_skill_name(payload.skill_name)
    if not skill_id:
        raise HTTPException(status_code=400, detail="skill_name is invalid")
    try:
        store = _skill_store(request)
        store.import_source(skill_id, skill_id, payload.source)
        result = store.install_to_client(
            Path.home(), skill_id, client_id, mode=payload.mode
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.system_terminal_sink.write(
        f"Agent Skill 本地安装完成：{result['client_id']} / {result['skill_name']}\n"
    )
    return result


@router.post("/skills/local/update", response_model=dict[str, object])
def update_local_skill(
    payload: AgentSkillLocalActionRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "skills")
    skill_id = safe_skill_name(payload.skill_name)
    if not skill_id:
        raise HTTPException(status_code=400, detail="skill_name is invalid")
    try:
        store = _skill_store(request)
        current = store.get(skill_id)
        if current is None:
            legacy = update_skill_to_home(
                Path.home(),
                client_id,
                skill_id,
                source=payload.source,
                mode=payload.mode,
            )
            store.import_from_client(Path.home(), client_id, skill_id)
            result = legacy
        else:
            source = payload.source or str(current.get("source") or "")
            if source:
                store.import_source(
                    skill_id,
                    str(current.get("name") or skill_id),
                    source,
                    description=current.get("description"),
                    version=current.get("version"),
                    metadata=dict(current.get("metadata") or {}),
                )
            result = store.install_to_client(
                Path.home(),
                skill_id,
                client_id,
                mode=payload.mode or "copy",
            )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.system_terminal_sink.write(
        f"Agent Skill 本地更新完成：{result['client_id']} / {result['skill_name']}\n"
    )
    return result


@router.post("/skills/local/uninstall", response_model=dict[str, object])
def uninstall_local_skill(
    payload: AgentSkillLocalActionRequest, request: Request
) -> dict[str, object]:
    require_authenticated_request(request)
    client_id = _require_local_client(payload.client_id, "skills")
    skill_id = safe_skill_name(payload.skill_name)
    if not skill_id:
        raise HTTPException(status_code=400, detail="skill_name is invalid")
    try:
        store = _skill_store(request)
        if store.get(skill_id) is None:
            result = uninstall_skill_from_home(Path.home(), client_id, skill_id)
        else:
            result = store.uninstall_from_client(Path.home(), skill_id, client_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    request.app.state.system_terminal_sink.write(
        f"Agent Skill 本地卸载完成：{result['client_id']} / {result['skill_name']}\n"
    )
    return result


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


def _build_resource_plan(
    request: Request, payload: AgentResourceRequest
) -> AgentResourcePlan:
    if payload.node_id != "__local__":
        raise HTTPException(status_code=400, detail="当前版本只执行本地 WSL 资源计划")
    reconciler = AgentReconciler(agent_data_root(request.app.state.config_root))
    if payload.resource_type in {None, 'mcp'} or payload.profile_id:
        store = _mcp_store(request)
        clients = [payload.client_id] if payload.client_id else list(CLIENT_PATHS)
        for client_id in clients:
            if client_id in CLIENT_PATHS:
                try:
                    store.refresh_observations(Path.home(), client_id)
                except ValueError:
                    # Persisted scan errors become plan warnings, not blind writes.
                    pass
    if payload.profile_id:
        try:
            return reconciler.plan_profile(
                payload.profile_id,
                node_id=payload.node_id,
                client_id=_safe_id(payload.client_id) if payload.client_id else None,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if payload.action == "sync":
        selected_types = {payload.resource_type} if payload.resource_type else None
        plan = reconciler.plan_assignments(
            node_id=payload.node_id,
            client_id=_safe_id(payload.client_id) if payload.client_id else None,
            resource_types=selected_types,
        )
        if payload.resource_ids:
            selected_ids = {item.strip() for item in payload.resource_ids if item.strip()}
            plan = AgentResourcePlan(
                operations=[
                    item for item in plan.operations if item.resource_id in selected_ids
                ],
                already_consistent=[
                    item
                    for item in plan.already_consistent
                    if item.rsplit(":", 1)[-1] in selected_ids
                ],
                warnings=list(plan.warnings),
            )
        return plan
    if payload.resource_type is None or payload.client_id is None:
        raise HTTPException(
            status_code=400,
            detail="resource_type and client_id are required",
        )
    resource_ids = [item.strip() for item in payload.resource_ids if item.strip()]
    if not resource_ids:
        raise HTTPException(status_code=400, detail="resource_ids is required")
    feature = {
        "provider": "providers",
        "mcp": "mcp",
        "skill": "skills",
        "prompt": "prompts",
        "router": "route",
    }[payload.resource_type]
    client_id = _require_local_client(payload.client_id, feature)
    return reconciler.plan_resources(
        payload.resource_type,
        resource_ids,
        client_id,
        action=payload.action,
        node_id=payload.node_id,
    )


def _reconcile_resource_request(
    request: Request, payload: AgentResourceRequest
) -> dict[str, object]:
    plan = _build_resource_plan(request, payload)
    results: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    for operation in plan.operations:
        try:
            result = _execute_resource_operation(request, operation)
        except Exception as exc:
            failure = {
                "action": operation.action,
                "resource_type": operation.resource_type,
                "resource_id": operation.resource_id,
                "client_id": operation.client_id,
                "error": _operation_error(exc),
            }
            failures.append(failure)
            results.append({**failure, "verified": False})
            continue
        normalized = {
            **result,
            "action": operation.action,
            "resource_type": operation.resource_type,
            "resource_id": operation.resource_id,
            "client_id": operation.client_id,
        }
        results.append(normalized)
        if not bool(result.get("verified")):
            failures.append({**normalized, "error": "客户端回读未通过"})
            continue
        if operation.action == "install":
            added.append(operation.resource_id)
        elif operation.action == "update":
            updated.append(operation.resource_id)
        else:
            removed.append(operation.resource_id)

    verified = not failures and not plan.warnings
    if verified:
        if payload.profile_id:
            _align_profile_assignments(request, payload)
        elif payload.action in {"install", "update", "uninstall"}:
            _align_explicit_assignments(request, payload)
    response = plan.as_dict()
    response.update(
        {
            "results": results,
            "failures": failures,
            "verified": verified,
            "added": sorted(set(added)),
            "updated": sorted(set(updated)),
            "removed": sorted(set(removed)),
        }
    )
    return response


def _execute_resource_operation(
    request: Request, operation: AgentResourceOperation
) -> dict[str, object]:
    if operation.node_id != "__local__":
        raise ValueError("当前版本只执行本地 WSL 资源操作")
    if operation.resource_type == "mcp":
        result = _run_local_mcp_operation(
            request,
            AgentMcpLocalRequest(
                client_id=operation.client_id,
                mcp_ids=[operation.resource_id],
            ),
            action="uninstall" if operation.action == "uninstall" else "install",
        )
        return dict(result)
    if operation.resource_type == "skill":
        store = _skill_store(request)
        if operation.action == "uninstall":
            return store.uninstall_from_client(
                Path.home(), operation.resource_id, operation.client_id
            )
        return store.install_to_client(
            Path.home(), operation.resource_id, operation.client_id
        )
    if operation.resource_type == "prompt":
        store = _prompt_store(request)
        if operation.action != "uninstall":
            return store.install_local(
                Path.home(), operation.client_id, operation.resource_id
            )
        try:
            return store.restore_local(Path.home(), operation.client_id)
        except FileNotFoundError:
            store.state.remove_prompt_assignment("__local__", operation.client_id)
            store.state.clear_prompt_observations("__local__", operation.client_id)
            return {
                "client_id": operation.client_id,
                "prompt_id": operation.resource_id,
                "retained_current_file": True,
                "verified": True,
            }
    if operation.resource_type == "provider":
        if operation.action == "uninstall":
            raise ValueError("Provider 使用切换或从库删除，不执行卸载")
        active = _active_takeover_apps(request, [operation.client_id])
        if active:
            raise ValueError(_takeover_conflict_message(active))
        store = _provider_store(request)
        include_secrets = bool((operation.config or {}).get("write_secrets"))
        provider = store.provider_for_apply(
            operation.client_id,
            operation.resource_id,
            include_secrets=include_secrets,
        )
        if provider is None:
            raise FileNotFoundError(f"Provider 不存在: {operation.resource_id}")
        shell = build_provider_apply_shell(
            provider,
            windows=False,
            write_secrets=include_secrets,
        )
        if not shell:
            raise ValueError(f"{operation.client_id} 暂不支持 Provider 写入")
        with provider_projection(_provider_projection(request, provider, write_secrets=include_secrets)):
            completed = subprocess.run(
                ["bash", "-lc", shell],
                cwd=Path.cwd(),
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("Provider 客户端写入失败")
        return {
            "client_id": operation.client_id,
            "provider_id": operation.resource_id,
            "verified": True,
        }
    if operation.resource_type == "router":
        if operation.action == "uninstall":
            raise ValueError("Router Profile 不执行卸载")
        return _apply_router_profile_item(request, operation)
    raise ValueError(f"不支持的资源类型: {operation.resource_type}")


def _apply_router_profile_item(
    request: Request, operation: AgentResourceOperation
) -> dict[str, object]:
    config = dict(operation.config or {})
    controller = request.app.state.agent_router_controller
    store = controller.store
    global_keys = {
        key: config[key]
        for key in (
            "listen_address",
            "listen_port",
            "show_home_switch",
            "outbound_proxy",
        )
        if key in config
    }
    if global_keys:
        store.update_global(**global_keys)
    provider_id = str(config.get("provider_id") or "").strip()
    if provider_id:
        runtime = _provider_store(request).runtime_profile(
            operation.client_id, provider_id
        )
        if runtime is None:
            raise FileNotFoundError(f"Router Provider 不存在: {provider_id}")
        store.set_provider(
            operation.client_id, runtime, provider_id=provider_id
        )
    queue = config.get("failover_queue")
    if isinstance(queue, list):
        store.set_failover_queue(
            operation.client_id, [str(item) for item in queue]
        )
    policy = config.get("policy")
    if isinstance(policy, dict) and policy:
        store.update_provider_policy(operation.client_id, **policy)
    if "takeover" in config:
        if bool(config["takeover"]):
            controller.enable_takeover(operation.client_id)
        else:
            controller.disable_takeover(operation.client_id)
    return {
        "client_id": operation.client_id,
        "router_id": operation.resource_id,
        "verified": True,
    }


def _align_explicit_assignments(
    request: Request, payload: AgentResourceRequest
) -> None:
    if payload.resource_type not in {"mcp", "skill", "prompt"} or not payload.client_id:
        return
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    client_id = _safe_id(payload.client_id)
    for resource_id in {item.strip() for item in payload.resource_ids if item.strip()}:
        if payload.action == "uninstall":
            _remove_resource_assignment(
                state, payload.resource_type, resource_id, "__local__", client_id
            )
            continue
        _set_resource_assignment(
            request,
            state,
            payload.resource_type,
            resource_id,
            client_id,
        )


def _align_profile_assignments(
    request: Request, payload: AgentResourceRequest
) -> None:
    if not payload.profile_id:
        return
    profile = _profile_store(request).get(payload.profile_id)
    if profile is None:
        return
    items = [
        item
        for item in profile.get("items", [])
        if payload.client_id is None or item["client_id"] == _safe_id(payload.client_id)
    ]
    clients = {str(item["client_id"]) for item in items}
    if payload.client_id:
        clients.add(_safe_id(payload.client_id))
    state = PanelStateStore(agent_data_root(request.app.state.config_root))
    for client_id in clients:
        client_items = [item for item in items if item["client_id"] == client_id]
        mcp_assignments: list[dict[str, object]] = []
        skill_assignments: list[dict[str, object]] = []
        prompt_id: str | None = None
        for item in client_items:
            resource_type = str(item["resource_type"])
            resource_id = str(item["resource_id"])
            if resource_type == "mcp":
                resolved = _mcp_store(request).resolve_server_for_client(
                    resource_id, client_id, "linux"
                )
                if resolved is not None:
                    mcp_assignments.append(
                        {
                            "mcp_id": resource_id,
                            "variant_client_id": resolved.get(
                                "variant_client_id", client_id
                            ),
                            "variant_platform": resolved.get(
                                "variant_platform", "any"
                            ),
                        }
                    )
            elif resource_type == "skill":
                skill_assignments.append(
                    {
                        "skill_id": resource_id,
                        "variant_client_id": client_id,
                        "variant_platform": "linux",
                    }
                )
            elif resource_type == "prompt":
                prompt_id = resource_id
        state.replace_mcp_assignments("__local__", client_id, mcp_assignments)
        state.replace_skill_assignments("__local__", client_id, skill_assignments)
        if prompt_id:
            state.set_prompt_assignment("__local__", client_id, prompt_id)
        else:
            state.remove_prompt_assignment("__local__", client_id)


def _set_resource_assignment(
    request: Request,
    state: PanelStateStore,
    resource_type: str,
    resource_id: str,
    client_id: str,
) -> None:
    if resource_type == "mcp":
        resolved = _mcp_store(request).resolve_server_for_client(
            resource_id, client_id, "linux"
        )
        if resolved is not None:
            state.set_mcp_assignment(
                "__local__",
                client_id,
                resource_id,
                variant_client_id=str(
                    resolved.get("variant_client_id") or client_id
                ),
                variant_platform=str(
                    resolved.get("variant_platform") or "any"
                ),
            )
    elif resource_type == "skill":
        state.set_skill_assignment("__local__", client_id, resource_id)
    elif resource_type == "prompt":
        state.set_prompt_assignment("__local__", client_id, resource_id)


def _resource_assignments(
    state: PanelStateStore, resource_type: str, resource_id: str
) -> list[dict[str, object]]:
    if resource_type == "mcp":
        return [
            item
            for item in state.list_mcp_assignments()
            if item["mcp_id"] == resource_id
        ]
    if resource_type == "skill":
        return [
            item
            for item in state.list_skill_assignments()
            if item["skill_id"] == resource_id
        ]
    return [
        item
        for item in state.list_prompt_assignments()
        if item["prompt_id"] == resource_id
    ]


def _resource_observed_present(
    state: PanelStateStore,
    resource_type: str,
    resource_id: str,
    node_id: str,
    client_id: str,
) -> bool:
    if resource_type == "mcp":
        rows = state.list_mcp_observations(node_id, client_id)
        id_key = "mcp_id"
    elif resource_type == "skill":
        rows = state.list_skill_observations(node_id, client_id)
        id_key = "skill_id"
    else:
        rows = state.list_prompt_observations(node_id, client_id)
        id_key = "prompt_id"
    return any(
        item[id_key] == resource_id and bool(item.get("present"))
        for item in rows
    )


def _remove_resource_assignment(
    state: PanelStateStore,
    resource_type: str,
    resource_id: str,
    node_id: str,
    client_id: str,
) -> None:
    if resource_type == "mcp":
        state.remove_mcp_assignments(node_id, client_id, {resource_id})
    elif resource_type == "skill":
        state.remove_skill_assignments(node_id, client_id, {resource_id})
    else:
        state.remove_prompt_assignment(node_id, client_id)


def _operation_error(exc: Exception) -> object:
    if isinstance(exc, HTTPException):
        return exc.detail
    return str(exc)


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
    projection: ProviderProjection | None = None,
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
                provider_projection=projection if node_id == "__local__" else None,
                command_timeout_seconds=120,
                working_dir=str(Path.cwd()),
            ),
        )
        tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"Agent 任务已入队：{action} · {len(tasks)} 个，跳过 {len(skipped)} 个\n"
    )
    return AgentQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


def _provider_projection(request: Request, provider: dict, *, operation: str = "apply", write_secrets: bool = True) -> ProviderProjection:
    return ProviderProjection(
        data_root=str(agent_data_root(request.app.state.config_root).resolve()),
        home=str(Path.home()), client_id=provider["app_id"], provider_id=provider["id"],
        operation=operation, provider=provider, write_secrets=write_secrets,
    )


def _mcp_store(request: Request) -> AgentMcpStore:
    return AgentMcpStore(agent_data_root(request.app.state.config_root))


def _skill_store(request: Request) -> AgentSkillStore:
    return AgentSkillStore(agent_data_root(request.app.state.config_root))


def _profile_store(request: Request) -> AgentProfileStore:
    return AgentProfileStore(agent_data_root(request.app.state.config_root))


def _prompt_store(request: Request) -> AgentPromptStore:
    return AgentPromptStore(agent_data_root(request.app.state.config_root))


def _provider_store(request: Request) -> AgentProviderStore:
    return AgentProviderStore(agent_data_root(request.app.state.config_root))


def _active_takeover_apps(request: Request, apps: list[str]) -> set[str]:
    controller = getattr(request.app.state, "agent_router_controller", None)
    if controller is None:
        return set()
    active = controller.active_takeover_clients()
    return {app_id for app_id in apps if app_id in active}


def _takeover_conflict_message(apps: set[str]) -> str:
    names = ", ".join(sorted(apps))
    return f"Router 正在接管 {names}，请先关闭接管并恢复客户端配置"


def _group_rows(
    rows: list[dict[str, object]], key: str
) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in rows:
        grouped.setdefault(str(item[key]), []).append(item)
    return grouped


def _public_profile(profile: dict[str, object]) -> dict[str, object]:
    return redact_sensitive(profile)


def _public_observation(item: dict[str, object]) -> dict[str, object]:
    safe = dict(item)
    safe.pop("public_spec_json", None)
    if isinstance(safe.get("public_spec"), dict):
        safe["public_spec"] = redact_sensitive(dict(safe["public_spec"]))
    return safe


def _skill_discovery(
    home: Path, managed: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
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


def _require_local_client(value: str, feature: str) -> str:
    client_id = _safe_id(value)
    if not client_id or client_id not in _supported_apps(feature):
        raise HTTPException(
            status_code=400,
            detail=f"{client_id or value} 暂不支持本地 {feature} 写入",
        )
    return client_id


def _safe_id(value: str) -> str:
    return "".join(
        ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip()
    ).strip("-")


def _safe_apps(values: list[str], *, feature: str | None = None) -> list[str]:
    supported = _supported_apps(feature)
    return [app_id for value in values if (app_id := _safe_id(value)) in supported]


def _supported_apps(feature: str | None = None) -> set[str]:
    from app.services.agent_clients import AGENT_CLIENTS

    return {
        client.id
        for client in AGENT_CLIENTS
        if feature is None or feature in client.write_support
    }
