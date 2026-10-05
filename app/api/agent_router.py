from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.core.security import require_authenticated_request
from app.services.agent_mcp import agent_data_root
from app.services.agent_providers import AgentProviderStore
from app.services.agent_router_config import AgentRouterConfigError, AgentRouterConfigStore
from app.services.agent_router_control import (
    ActiveTakeoverError,
    AgentRouterControlError,
    AgentRouterController,
)

router = APIRouter(prefix="/api/agent/router", tags=["agent-router"])


class AgentRouterConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen_address: str | None = None
    listen_port: int | None = Field(default=None, ge=1, le=65535)
    show_home_switch: bool | None = None
    outbound_proxy: str | None = None


class AgentRouterLifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restore_clients: bool = False


class AgentRouterTakeoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AgentRouterProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: dict[str, Any]


class AgentRouterProviderSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_id: str


class AgentRouterPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_failover: bool | None = None
    max_retries: int | None = Field(default=None, ge=0, le=10)
    failure_threshold: int | None = Field(default=None, ge=1, le=100)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=86400)


class AgentRouterFailoverQueueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_ids: list[str] = Field(default_factory=list, max_length=100)


@router.get("/status", response_model=dict[str, Any])
def router_status(request: Request) -> dict[str, Any]:
    require_authenticated_request(request)
    return _controller(request).status()


@router.put("/config", response_model=dict[str, Any])
def update_router_config(
    payload: AgentRouterConfigRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    values: dict[str, Any] = {}
    for key in payload.model_fields_set:
        values[key] = getattr(payload, key)
    try:
        _controller(request).configure(**values)
    except (AgentRouterConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"config": _controller(request).store.public_snapshot()}


@router.post("/start", response_model=dict[str, Any])
def start_router(request: Request) -> dict[str, Any]:
    require_authenticated_request(request)
    return _lifecycle(request, "start")


@router.post("/restart", response_model=dict[str, Any])
def restart_router(request: Request) -> dict[str, Any]:
    require_authenticated_request(request)
    return _lifecycle(request, "restart")


@router.post("/stop", response_model=dict[str, Any])
def stop_router(
    payload: AgentRouterLifecycleRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    controller = _controller(request)
    try:
        result = controller.stop(restore_clients=payload.restore_clients)
    except ActiveTakeoverError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "active client takeovers", "clients": exc.clients},
        ) from exc
    except AgentRouterControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.get("/providers", response_model=dict[str, Any])
def router_providers(request: Request) -> dict[str, Any]:
    require_authenticated_request(request)
    return {"providers": _controller(request).store.public_snapshot()["providers"]}


@router.put("/providers/{client_id}", response_model=dict[str, Any])
def set_router_provider(
    client_id: str, payload: AgentRouterProviderRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    try:
        saved = _controller(request).store.set_provider(client_id, payload.profile)
    except (AgentRouterConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"provider": _redact_profile(saved)}


@router.put("/apps/{client_id}/provider", response_model=dict[str, Any])
def select_client_provider(
    client_id: str, payload: AgentRouterProviderSelectionRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    clean_client = client_id.strip()
    clean_provider = payload.provider_id.strip()
    if not clean_client or not clean_provider:
        raise HTTPException(status_code=400, detail="client_id and provider_id are required")
    data_root = agent_data_root(request.app.state.config_root)
    provider_store = AgentProviderStore(data_root)
    rows = provider_store.list_providers(clean_client).get(clean_client, [])
    ordered_rows = sorted(
        rows,
        key=lambda row: (0 if str(row.get("id")) == clean_provider else 1, str(row.get("name") or row.get("id") or "").lower()),
    )
    profiles: list[dict[str, Any]] = []
    for row in ordered_rows:
        provider_id = str(row.get("id") or "").strip()
        runtime = provider_store.runtime_profile(clean_client, provider_id)
        if runtime is None:
            continue
        runtime["provider_id"] = provider_id
        profiles.append(runtime)
    if not profiles or profiles[0].get("provider_id") != clean_provider:
        raise HTTPException(status_code=404, detail="provider profile not found")
    profile = dict(profiles[0])
    profile.pop("provider_id", None)
    existing = _controller(request).store.get_provider(clean_client) or {}
    for key in ("auto_failover", "max_retries", "failure_threshold", "cooldown_seconds"):
        if key in existing:
            profile[key] = existing[key]
    profile["fallbacks"] = [
        {key: value for key, value in item.items() if key != "provider_id"} | {"provider_id": item["provider_id"]}
        for item in profiles[1:]
    ]
    try:
        saved = _controller(request).store.set_provider(
            clean_client, profile, provider_id=clean_provider
        )
    except (AgentRouterConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    provider_store.set_current(clean_client, clean_provider)
    return {
        "client_id": clean_client,
        "provider_id": clean_provider,
        "provider": _redact_profile(saved),
    }


@router.get("/apps/{client_id}/failover-queue", response_model=dict[str, Any])
def get_client_failover_queue(client_id: str, request: Request) -> dict[str, Any]:
    require_authenticated_request(request)
    clean_client = client_id.strip()
    if not clean_client:
        raise HTTPException(status_code=400, detail="client_id is required")
    provider_store = AgentProviderStore(agent_data_root(request.app.state.config_root))
    rows = provider_store.list_providers(clean_client).get(clean_client, [])
    by_id = {str(row.get("id")): row for row in rows}
    queue = _controller(request).store.get_failover_queue(clean_client)
    items = [
        {
            "provider_id": provider_id,
            "name": str(by_id[provider_id].get("name") or provider_id),
            "is_current": bool(by_id[provider_id].get("is_current")),
        }
        for provider_id in queue
        if provider_id in by_id
    ]
    available = [
        {"provider_id": provider_id, "name": str(row.get("name") or provider_id)}
        for provider_id, row in by_id.items()
        if provider_id not in queue
    ]
    return {"client_id": clean_client, "provider_ids": queue, "items": items, "available": available}


@router.put("/apps/{client_id}/failover-queue", response_model=dict[str, Any])
def set_client_failover_queue(
    client_id: str, payload: AgentRouterFailoverQueueRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    clean_client = client_id.strip()
    if not clean_client:
        raise HTTPException(status_code=400, detail="client_id is required")
    provider_store = AgentProviderStore(agent_data_root(request.app.state.config_root))
    rows = provider_store.list_providers(clean_client).get(clean_client, [])
    by_id = {str(row.get("id")): row for row in rows}
    provider_ids = [str(item).strip() for item in payload.provider_ids if str(item).strip()]
    if len(set(provider_ids)) != len(provider_ids):
        raise HTTPException(status_code=400, detail="provider_ids must be unique")
    missing = [provider_id for provider_id in provider_ids if provider_id not in by_id]
    if missing:
        raise HTTPException(status_code=404, detail=f"provider not found: {missing[0]}")

    store = _controller(request).store
    snapshot = store.snapshot()
    current_id = str((snapshot.get("provider_ids") or {}).get(clean_client) or "").strip()
    if not current_id:
        current_id = next(
            (str(row.get("id")) for row in rows if bool(row.get("is_current"))),
            "",
        )
    if not current_id or current_id not in by_id:
        raise HTTPException(status_code=409, detail="先选择当前 Provider")

    runtimes: dict[str, dict[str, Any]] = {}
    for provider_id in {current_id, *provider_ids}:
        runtime = provider_store.runtime_profile(clean_client, provider_id)
        if runtime is None:
            raise HTTPException(status_code=400, detail=f"provider profile invalid: {provider_id}")
        runtimes[provider_id] = runtime

    queue = store.set_failover_queue(clean_client, provider_ids)
    _save_router_provider_catalog(
        store, clean_client, current_id, runtimes, queue, existing=snapshot.get("providers", {}).get(clean_client)
    )
    return {
        "client_id": clean_client,
        "provider_ids": queue,
        "items": [
            {
                "provider_id": provider_id,
                "name": str(by_id[provider_id].get("name") or provider_id),
                "is_current": provider_id == current_id,
            }
            for provider_id in queue
        ],
    }


@router.put("/apps/{client_id}/policy", response_model=dict[str, Any])
def update_client_policy(
    client_id: str, payload: AgentRouterPolicyRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    clean_client = client_id.strip()
    if not clean_client:
        raise HTTPException(status_code=400, detail="client_id is required")
    store = _controller(request).store
    current = store.get_provider(clean_client)
    if current is None:
        raise HTTPException(status_code=404, detail="router provider not configured")
    changes = {
        key: getattr(payload, key)
        for key in payload.model_fields_set
        if getattr(payload, key) is not None
    }
    if changes.get("auto_failover") is True:
        current_id = str((store.snapshot().get("provider_ids") or {}).get(clean_client) or "").strip()
        if not current_id:
            raise HTTPException(status_code=409, detail="先选择当前 Provider")
        if clean_client not in (store.snapshot().get("failover_queues") or {}):
            store.set_failover_queue(clean_client, [current_id])
        elif not store.get_failover_queue(clean_client):
            store.set_failover_queue(clean_client, [current_id])
    try:
        saved = store.update_provider_policy(clean_client, **changes)
    except (AgentRouterConfigError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"client_id": clean_client, "provider": _redact_profile(saved)}


@router.put("/apps/{client_id}/takeover", response_model=dict[str, Any])
def set_client_takeover(
    client_id: str, payload: AgentRouterTakeoverRequest, request: Request
) -> dict[str, Any]:
    require_authenticated_request(request)
    controller = _controller(request)
    try:
        result = (
            controller.enable_takeover(client_id)
            if payload.enabled
            else controller.disable_takeover(client_id)
        )
    except AgentRouterControlError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "takeover": {client_id: payload.enabled},
        "details": result,
    }


def _save_router_provider_catalog(
    store: AgentRouterConfigStore,
    client_id: str,
    current_id: str,
    runtimes: dict[str, dict[str, Any]],
    queue: list[str],
    *,
    existing: Any = None,
) -> dict[str, Any]:
    primary = dict(runtimes[current_id])
    if isinstance(existing, dict):
        for key in ("auto_failover", "max_retries", "failure_threshold", "cooldown_seconds"):
            if key in existing:
                primary[key] = existing[key]
    primary["fallbacks"] = [
        {**dict(runtimes[provider_id]), "provider_id": provider_id}
        for provider_id in queue
        if provider_id != current_id and provider_id in runtimes
    ]
    return store.set_provider(client_id, primary, provider_id=current_id)


def _lifecycle(request: Request, action: str) -> dict[str, Any]:
    controller = _controller(request)
    result = getattr(controller, action)()
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail="router service command failed")
    return result


def _controller(request: Request) -> AgentRouterController:
    current = getattr(request.app.state, "agent_router_controller", None)
    if current is not None:
        return current
    store = AgentRouterConfigStore(agent_data_root(request.app.state.config_root))
    current = AgentRouterController(store)
    request.app.state.agent_router_controller = current
    return current


def _redact_profile(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in ("api_key", "apikey", "token", "secret", "password", "credential")):
                result[str(key)] = "••••••••"
            elif lowered == "headers" and isinstance(child, dict):
                result[str(key)] = {str(header): "••••••••" for header in child}
            else:
                result[str(key)] = _redact_profile(child)
        return result
    if isinstance(value, list):
        return [_redact_profile(item) for item in value]
    return value
