from typing import Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import ActionPlan
from app.adapters.docker_adapter import DockerComposeAdapter, detect_image_repository_from_compose, parse_image_repository
from app.adapters.node_adapter import NodePackageAdapter
from app.adapters.python_adapter import PythonPackageAdapter
from app.adapters.systemd_adapter import SystemdUnitAdapter
from app.core.security import require_authenticated_request
from app.models.assets import AssetSnapshot, PackageVersionInfo, RuntimeVersionInfo
from app.models.registry import ObjectDefinition
from app.models.tasks import TaskRecord

router = APIRouter(prefix='/api/assets', tags=['assets'])


class ActionAdapter(Protocol):
    def plan_action(self, action: str, version: str | None = None) -> ActionPlan: ...

    def list_available_versions(self) -> list[str]: ...

    def get_version_info(self) -> PackageVersionInfo: ...


class DeployVersionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    version: str


class AssetActionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    task: TaskRecord
    plan: ActionPlan


class AssetVersionsResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    object_id: str
    current_version: str | None = None
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = 'ok'
    error: str | None = None
    lifecycle_strategy: str | None = None
    version_source: str | None = None
    runtime: RuntimeVersionInfo | None = None
    managed_services: list[str] = Field(default_factory=list)
    ignored_services: list[str] = Field(default_factory=list)


@router.get('/{object_id}/versions', response_model=AssetVersionsResponse)
def get_asset_versions(object_id: str, request: Request) -> AssetVersionsResponse:
    asset = _get_asset(request, object_id)
    adapter = _build_adapter(request, object_id, asset)
    version_info = (
        adapter.get_version_info()
        if hasattr(adapter, 'get_version_info')
        else PackageVersionInfo(current_version=asset.current_version, versions=adapter.list_available_versions())
    )
    return AssetVersionsResponse(
        object_id=object_id,
        current_version=version_info.current_version,
        latest_version=version_info.latest_version,
        versions=version_info.versions,
        source_status=version_info.source_status,
        error=version_info.error,
        lifecycle_strategy=version_info.lifecycle_strategy,
        version_source=version_info.version_source,
        runtime=version_info.runtime,
        managed_services=version_info.managed_services,
        ignored_services=version_info.ignored_services,
    )


@router.post('/{object_id}/actions/update-latest', status_code=202, response_model=AssetActionResponse)
def queue_update_latest(object_id: str, request: Request):
    return _queue_action(request, object_id, action='update_latest')


@router.post('/{object_id}/actions/deploy-version', status_code=202, response_model=AssetActionResponse)
async def queue_deploy_version(object_id: str, request: Request):
    resolved_version = await _extract_requested_version(request)
    if not resolved_version:
        raise HTTPException(status_code=400, detail='version is required')
    return _queue_action(request, object_id, action='deploy_version', version=resolved_version)


@router.post('/{object_id}/actions/delete', status_code=202, response_model=AssetActionResponse)
def queue_delete(object_id: str, request: Request):
    return _queue_action(request, object_id, action='delete')


@router.get('/{object_id}/actions/full-delete-preview', response_model=ActionPlan)
def full_delete_preview(object_id: str, request: Request) -> ActionPlan:
    asset = _get_asset(request, object_id)
    adapter = _build_adapter(request, object_id, asset)
    try:
        return adapter.plan_action('full_delete')
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post('/{object_id}/actions/full-delete', status_code=202, response_model=AssetActionResponse)
def queue_full_delete(object_id: str, request: Request):
    return _queue_action(request, object_id, action='full_delete')


def get_page_asset(request: Request, object_id: str) -> AssetSnapshot:
    return _get_asset(request, object_id)


def get_page_asset_versions(request: Request, object_id: str, asset: AssetSnapshot) -> list[str]:
    try:
        adapter = _build_adapter(request, object_id, asset)
    except HTTPException:
        return []
    return adapter.list_available_versions()


def get_page_asset_version_info(request: Request, object_id: str, asset: AssetSnapshot) -> PackageVersionInfo | None:
    try:
        adapter = _build_adapter(request, object_id, asset)
    except HTTPException:
        return None
    if hasattr(adapter, 'get_version_info'):
        return adapter.get_version_info()
    return None


async def _extract_requested_version(request: Request) -> str | None:
    content_type = request.headers.get('content-type', '')
    if content_type.startswith('application/json'):
        payload = await request.json()
        if isinstance(payload, dict):
            value = payload.get('version')
            return value if isinstance(value, str) and value else None
        return None

    form = await request.form()
    value = form.get('version')
    return value if isinstance(value, str) and value else None


def _queue_action(request: Request, object_id: str, *, action: str, version: str | None = None):
    asset = _get_asset(request, object_id)
    adapter = _build_adapter(request, object_id, asset)
    try:
        plan = adapter.plan_action(action, version=version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task = request.app.state.task_queue.enqueue(object_id, action, requested_version=version, plan=plan)
    if request.headers.get('hx-request') == 'true':
        return _render_task_flash(task, action, version)
    return AssetActionResponse(task=task, plan=plan)


def _render_task_flash(task: TaskRecord, action: str, version: str | None) -> HTMLResponse:
    details = f' · 版本 {version}' if version else ''
    markup = (
        '<div class="flash flash-success">'
        f'任务已入队：{task.object_id} · {action}{details} · {task.id}'
        '</div>'
    )
    return HTMLResponse(markup, status_code=202)


def _get_asset(request: Request, object_id: str) -> AssetSnapshot:
    require_authenticated_request(request)
    asset = request.app.state.asset_service.get_asset(object_id)
    if asset is None:
        raise HTTPException(status_code=404, detail='asset not found')
    return asset


def _build_adapter(request: Request, object_id: str, asset: AssetSnapshot) -> ActionAdapter:
    if asset.category == 'node':
        _ensure_asset_actionable(asset)
        return NodePackageAdapter(
            package_name=asset.name,
            current_version=asset.current_version,
            full_delete_paths=list(asset.metadata.get('full_delete_paths', [])),
        )
    if asset.category == 'python':
        _ensure_asset_actionable(asset)
        return PythonPackageAdapter(
            package_name=asset.name,
            current_version=asset.current_version,
            full_delete_paths=list(asset.metadata.get('full_delete_paths', [])),
        )
    obj = _get_registry_object(request, object_id)
    if obj.type == 'docker_compose':
        return _build_docker_adapter(request, obj, asset)
    if obj.type == 'systemd_unit':
        return SystemdUnitAdapter(unit_name=obj.config['unit_name'], working_dir=obj.config['working_dir'])
    raise HTTPException(status_code=400, detail=f'unsupported object type: {obj.type}')


def _ensure_asset_actionable(asset: AssetSnapshot) -> None:
    if asset.actionable:
        return
    raise HTTPException(status_code=409, detail=asset.blocked_reason or 'asset is read only')


def _build_docker_adapter(request: Request, obj: ObjectDefinition, asset: AssetSnapshot) -> DockerComposeAdapter:
    primary_container_name = obj.config.get('primary_container') or asset.primary_container_name
    primary_container = next((item for item in asset.containers if item.name == asset.primary_container_name), None)
    configured_service = obj.config.get('compose_service')
    compose_service = configured_service or (primary_container.compose_service if primary_container else None)
    if not compose_service:
        raise HTTPException(status_code=400, detail='compose_service is required for docker actions')
    recipe_service = getattr(request.app.state, 'docker_recipe_service', None)
    recipe = recipe_service.get(obj.config.get('recipe_id')) if recipe_service is not None else None

    image_repository = parse_image_repository(primary_container.image if primary_container else None)
    if image_repository is None:
        image_repository = detect_image_repository_from_compose(
            obj.config['project_dir'],
            obj.config['compose_file'],
            compose_service,
        )
    primary_name = primary_container_name or compose_service

    return DockerComposeAdapter(
        project_dir=obj.config['project_dir'],
        compose_file=obj.config['compose_file'],
        primary_container=primary_name,
        compose_service=compose_service,
        image_repository=image_repository,
        current_version=asset.current_version,
        lifecycle_strategy=obj.config.get('lifecycle_strategy', 'compose_pull'),
        override_file=recipe.override_file if recipe is not None else None,
        recipe_repo_dir=recipe.repo_dir if recipe is not None else None,
        local_image_repository=recipe.local_image_repository if recipe is not None else None,
        local_image_tag_template=recipe.local_image_tag_template if recipe is not None else None,
        healthcheck_url=recipe.healthcheck.url if recipe is not None and recipe.healthcheck is not None else None,
        version_service=getattr(request.app.state, 'docker_version_service', None),
    )


def _get_registry_object(request: Request, object_id: str) -> ObjectDefinition:
    snapshot = request.app.state.registry_service.snapshot
    for obj in snapshot.objects:
        if obj.id == object_id:
            return obj
    raise HTTPException(status_code=404, detail='asset not found')
