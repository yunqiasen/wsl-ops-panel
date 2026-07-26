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
    profile = provider_store.runtime_profile(clean_client, clean_provider)
    if profile is None:
        raise HTTPException(status_code=404, detail="provider profile not found")
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
