from typing import Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import ActionPlan
from app.adapters.docker_adapter import DockerComposeAdapter, detect_image_repository_from_compose, parse_image_repository
from app.adapters.host_port_adapter import HostPortAdapter
from app.adapters.node_adapter import NodePackageAdapter
from app.adapters.project_adapter import ProjectAdapter
from app.adapters.python_adapter import PythonPackageAdapter
from app.adapters.systemd_adapter import SystemdUnitAdapter
from app.adapters.system_adapter import SystemInfrastructureAdapter
from app.core.security import require_authenticated_request
from app.models.assets import AssetSnapshot, PackageVersionInfo, RuntimeVersionInfo
from app.models.registry import ObjectDefinition
from app.models.tasks import TaskRecord
from app.services.notifications import asset_to_notification_json

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
    version_info = _get_asset_version_info_from_snapshot(asset)
    if version_info is None:
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
    return enqueue_asset_action(request, object_id, action='update_latest')


@router.post('/{object_id}/actions/start', status_code=202, response_model=AssetActionResponse)
def queue_start(object_id: str, request: Request):
    return enqueue_asset_action(request, object_id, action='start')


@router.post('/{object_id}/actions/stop', status_code=202, response_model=AssetActionResponse)
def queue_stop(object_id: str, request: Request):
    return enqueue_asset_action(request, object_id, action='stop')


@router.post('/{object_id}/actions/restart', status_code=202, response_model=AssetActionResponse)
def queue_restart(object_id: str, request: Request):
    return enqueue_asset_action(request, object_id, action='restart')


@router.post('/{object_id}/actions/deploy-version', status_code=202, response_model=AssetActionResponse)
async def queue_deploy_version(object_id: str, request: Request):
    resolved_version = await _extract_requested_version(request)
    if not resolved_version:
        raise HTTPException(status_code=400, detail='version is required')
    return enqueue_asset_action(request, object_id, action='deploy_version', version=resolved_version)


@router.post('/{object_id}/actions/delete', status_code=202, response_model=AssetActionResponse)
def queue_delete(object_id: str, request: Request):
    return enqueue_asset_action(request, object_id, action='delete')


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
    return enqueue_asset_action(request, object_id, action='full_delete')


def get_page_asset(request: Request, object_id: str) -> AssetSnapshot:
    return _get_asset(request, object_id)


def get_page_asset_versions(request: Request, object_id: str, asset: AssetSnapshot) -> list[str]:
    version_info = _get_asset_version_info_from_snapshot(asset)
    if version_info is not None:
        return version_info.versions
    try:
        adapter = _build_adapter(request, object_id, asset)
    except HTTPException:
        return []
    return adapter.list_available_versions()


def get_page_asset_version_info(request: Request, object_id: str, asset: AssetSnapshot) -> PackageVersionInfo | None:
    version_info = _get_asset_version_info_from_snapshot(asset)
    if version_info is not None:
        return version_info
    try:
        adapter = _build_adapter(request, object_id, asset)
    except HTTPException:
        return None
    if hasattr(adapter, 'get_version_info'):
        return adapter.get_version_info()
    return None


def _get_asset_version_info_from_snapshot(asset: AssetSnapshot) -> PackageVersionInfo | None:
    available_versions = asset.metadata.get('available_versions')
    if not isinstance(available_versions, list):
        return None
    source_status = str(asset.metadata.get('source_status') or asset.metadata.get('version_source_status') or 'ok')
    if source_status == 'deferred':
        return None

    runtime = _runtime_from_asset_metadata(asset)
    managed_services = asset.metadata.get('managed_services', [])
    ignored_services = asset.metadata.get('ignored_services', [])
    return PackageVersionInfo(
        current_version=asset.current_version,
        latest_version=asset.latest_version,
        versions=[version for version in available_versions if isinstance(version, str)],
        source_status=source_status,
        error=asset.metadata.get('error') if isinstance(asset.metadata.get('error'), str) else None,
        lifecycle_strategy=asset.metadata.get('lifecycle_strategy')
        if isinstance(asset.metadata.get('lifecycle_strategy'), str)
        else None,
        version_source=asset.metadata.get('version_source') if isinstance(asset.metadata.get('version_source'), str) else None,
        runtime=runtime,
        managed_services=[service for service in managed_services if isinstance(service, str)],
        ignored_services=[service for service in ignored_services if isinstance(service, str)],
    )


def _runtime_from_asset_metadata(asset: AssetSnapshot) -> RuntimeVersionInfo | None:
    runtime_payload = asset.metadata.get('runtime')
    if isinstance(runtime_payload, dict):
        return RuntimeVersionInfo.model_validate(runtime_payload)

    has_runtime_fields = any(
        asset.metadata.get(key) is not None
        for key in ('runtime_image_tag', 'runtime_oci_version', 'runtime_oci_revision')
    )
    if not has_runtime_fields and not asset.containers:
        return None

    primary_container = next((item for item in asset.containers if item.name == asset.primary_container_name), None)
    image = primary_container.image if primary_container is not None else None
    ports = primary_container.ports if primary_container is not None else None
    return RuntimeVersionInfo(
        image=image,
        image_tag=asset.metadata.get('runtime_image_tag'),
        oci_version=asset.metadata.get('runtime_oci_version'),
        oci_revision=asset.metadata.get('runtime_oci_revision'),
        ports=ports,
    )


def _project_current_version(asset: AssetSnapshot) -> str | None:
    for key in ('git_branch', 'head_sha'):
        value = asset.metadata.get(key)
        if isinstance(value, str) and value:
            return value[:12] if key == 'head_sha' else value
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


def enqueue_asset_action(request: Request, object_id: str, *, action: str, version: str | None = None,
                         asset: AssetSnapshot | None = None, render_flash: bool = True):
    require_authenticated_request(request)
    asset = asset if asset is not None else _get_asset(request, object_id)
    if asset.object_id != object_id:
        raise HTTPException(status_code=409, detail='asset selection changed; refresh and retry')
    if action not in asset.supports_actions:
        if not asset.actionable and asset.category in {'node', 'python', 'agent_cli'}:
            _ensure_asset_actionable(asset)
        raise HTTPException(status_code=400, detail=f'action {action} is not supported by {object_id}')
    adapter = _build_adapter(request, object_id, asset)
    try:
        plan = adapter.plan_action(action, version=version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task = request.app.state.task_queue.enqueue(object_id, action, requested_version=version, plan=plan)
    if render_flash and request.headers.get('hx-request') == 'true':
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
    if asset.category == 'system':
        return SystemInfrastructureAdapter(name=asset.name, current_version=asset.current_version)
    if asset.category == 'host':
        return HostPortAdapter(
            port=str(asset.metadata.get('port') or ''),
            owner_type=asset.metadata.get('owner_type') if isinstance(asset.metadata.get('owner_type'), str) else None,
            target_asset_id=asset.metadata.get('target_asset_id')
            if isinstance(asset.metadata.get('target_asset_id'), str)
            else None,
            target_unit_name=asset.metadata.get('target_unit_name')
            if isinstance(asset.metadata.get('target_unit_name'), str)
            else None,
            target_container_name=asset.metadata.get('target_container_name')
            if isinstance(asset.metadata.get('target_container_name'), str)
            else None,
            pid=asset.metadata.get('pid') if isinstance(asset.metadata.get('pid'), int) else None,
            process_name=asset.metadata.get('process_name') if isinstance(asset.metadata.get('process_name'), str) else None,
        )
    if asset.category == 'project':
        project_dir = asset.metadata.get('path')
        if not isinstance(project_dir, str) or not project_dir:
            raise HTTPException(status_code=400, detail='project path is required for project actions')
        cf = asset.metadata.get('capabilities', {}).get('cf_tunnel', {}) if isinstance(asset.metadata.get('capabilities'), dict) else {}
        return ProjectAdapter(
            project_dir=project_dir,
            current_version=asset.current_version or _project_current_version(asset),
            git_remote_url=asset.metadata.get('git_remote_url') if isinstance(asset.metadata.get('git_remote_url'), str) else None,
            service_unit=asset.metadata.get('service_unit') if isinstance(asset.metadata.get('service_unit'), str) else None,
            service_units=[item for item in asset.metadata.get('service_units', []) if isinstance(item, str)]
            if isinstance(asset.metadata.get('service_units'), list)
            else None,
            service_scope=asset.metadata.get('service_scope')
            if isinstance(asset.metadata.get('service_scope'), str)
            else 'system',
            cftunnel_unit=asset.metadata.get('cftunnel_unit') if isinstance(asset.metadata.get('cftunnel_unit'), str) else None,
            cftunnel_script=cf.get('script_path') if isinstance(cf, dict) and isinstance(cf.get('script_path'), str) else None,
            config_root=str(request.app.state.config_root),
            asset_snapshot_json=asset_to_notification_json(asset),
        )
    obj = _get_registry_object(request, object_id, required=False)
    if asset.category == 'docker' and obj is None:
        return _build_discovered_docker_adapter(request, asset)
    if obj is None:
        raise HTTPException(status_code=400, detail=f'unsupported discovered asset: {asset.category}')
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
    docker_version_service = getattr(request.app.state, 'docker_version_service', None)
    runtime = docker_version_service.build_runtime_version_info(primary_container) if docker_version_service else None
    managed_services = recipe.managed_services if recipe is not None else obj.config.get('managed_services', [])
    ignored_services = recipe.ignored_services if recipe is not None else obj.config.get('ignored_services', [])

    source_links = asset.metadata.get('source_links') if isinstance(asset.metadata.get('source_links'), dict) else {}
    source_url = (
        primary_container.labels.get('org.opencontainers.image.source')
        if primary_container is not None
        else None
    ) or (source_links.get('github') if isinstance(source_links.get('github'), str) else None)

    return DockerComposeAdapter(
        project_dir=obj.config['project_dir'],
        compose_file=obj.config['compose_file'],
        primary_container=primary_name,
        compose_service=compose_service,
        image_repository=image_repository,
        current_version=asset.current_version,
        source_url=source_url,
        lifecycle_strategy=obj.config.get('lifecycle_strategy', 'compose_pull'),
        override_file=recipe.override_file if recipe is not None else None,
        recipe_repo_dir=recipe.repo_dir if recipe is not None else None,
        local_image_repository=recipe.local_image_repository if recipe is not None else None,
        local_image_tag_template=recipe.local_image_tag_template if recipe is not None else None,
        build_worktree_dir=recipe.build_worktree_dir if recipe is not None else None,
        docker_build_args=recipe.docker_build_args if recipe is not None else None,
        healthcheck_url=recipe.healthcheck.url if recipe is not None and recipe.healthcheck is not None else None,
        runtime=runtime,
        managed_services=managed_services,
        ignored_services=ignored_services,
        version_service=docker_version_service,
        config_root=str(request.app.state.config_root),
        asset_snapshot_json=asset_to_notification_json(asset),
        runtime_context=_docker_runtime_context(asset, primary_container, obj.config),
    )


def _build_discovered_docker_adapter(request: Request, asset: AssetSnapshot) -> DockerComposeAdapter:
    primary_container = next((item for item in asset.containers if item.name == asset.primary_container_name), None)
    compose_service = str(asset.metadata.get('compose_service') or (primary_container.compose_service if primary_container else '')).strip()
    if not compose_service:
        raise HTTPException(status_code=400, detail='compose_service is required for docker actions')
    project_dir = str(asset.metadata.get('project_dir') or '')
    compose_file = str(asset.metadata.get('compose_file') or 'docker-compose.yml')
    if not project_dir:
        raise HTTPException(status_code=400, detail='project_dir is required for docker actions')
    image_repository = asset.metadata.get('image_repository')
    if not isinstance(image_repository, str):
        image_repository = parse_image_repository(primary_container.image if primary_container else None)
    docker_version_service = getattr(request.app.state, 'docker_version_service', None)
    runtime = docker_version_service.build_runtime_version_info(primary_container) if docker_version_service else None
    source_links = asset.metadata.get('source_links') if isinstance(asset.metadata.get('source_links'), dict) else {}
    source_url = (
        primary_container.labels.get('org.opencontainers.image.source')
        if primary_container is not None
        else None
    ) or (source_links.get('github') if isinstance(source_links.get('github'), str) else None)
    return DockerComposeAdapter(
        project_dir=project_dir,
        compose_file=compose_file,
        primary_container=asset.primary_container_name or compose_service,
        compose_service=compose_service,
        image_repository=image_repository,
        current_version=asset.current_version,
        source_url=source_url,
        lifecycle_strategy=str(asset.metadata.get('lifecycle_strategy') or 'compose_pull'),
        runtime=runtime,
        version_service=docker_version_service,
        config_root=str(request.app.state.config_root),
        asset_snapshot_json=asset_to_notification_json(asset),
        runtime_context=_docker_runtime_context(asset, primary_container, asset.metadata),
    )


def _get_registry_object(request: Request, object_id: str, *, required: bool = True) -> ObjectDefinition | None:
    snapshot = request.app.state.registry_service.snapshot
    for obj in snapshot.objects:
        if obj.id == object_id:
            return obj
    if required:
        raise HTTPException(status_code=404, detail='asset not found')
    return None


def _docker_runtime_context(asset, primary, config):
    labels = primary.labels if primary else {}
    files = config.get('compose_files') or [part for part in labels.get('com.docker.compose.project.config_files', '').split(',') if part]
    env_files = config.get('env_files') or [part for part in labels.get('com.docker.compose.project.environment_file', '').split(',') if part]
    project = config.get('compose_project') or (primary.compose_project if primary else None)
    return {
        'project': project,
        'compose_files': files or [config.get('compose_file') or 'docker-compose.yml'],
        'env_files': env_files,
        'containers': [{'id': c.id, 'name': c.name} for c in asset.containers if not project or c.compose_project == project],
    }
