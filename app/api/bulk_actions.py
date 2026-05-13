from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.assets import AssetActionResponse, enqueue_asset_action
from app.core.security import require_authenticated_request
from app.models.tasks import TaskRecord

router = APIRouter(prefix='/api/bulk/actions', tags=['bulk-actions'])

ACTION_MAP = {
    'update-latest': 'update_latest',
    'deploy-version': 'deploy_version',
    'delete': 'delete',
    'full-delete': 'full_delete',
    'start': 'start',
    'stop': 'stop',
    'autostart-enable': 'autostart_enable',
    'autostart-disable': 'autostart_disable',
    'cf-create': 'cf_create',
    'cf-refresh': 'cf_refresh',
    'cf-disable': 'cf_disable',
    'notify-send': 'notify_send',
}


class BulkActionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    asset_ids: list[str] = Field(min_length=1)
    version_map: dict[str, str] = Field(default_factory=dict)
    options: dict[str, str] = Field(default_factory=dict)


class BulkActionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: str
    queued_count: int
    tasks: list[TaskRecord]


@router.post('/{action_slug}', status_code=202, response_model=BulkActionResponse)
def queue_bulk_action(action_slug: str, payload: BulkActionRequest, request: Request) -> BulkActionResponse:
    require_authenticated_request(request)
    action = ACTION_MAP.get(action_slug)
    if action is None:
        raise HTTPException(status_code=404, detail='bulk action not found')

    tasks: list[TaskRecord] = []
    for asset_id in payload.asset_ids:
        version = payload.version_map.get(asset_id) if action == 'deploy_version' else None
        if action == 'deploy_version' and not version:
            raise HTTPException(status_code=400, detail=f'version is required for {asset_id}')
        response = enqueue_asset_action(request, asset_id, action=action, version=version)
        if not isinstance(response, AssetActionResponse):
            raise HTTPException(status_code=500, detail='unexpected action response')
        tasks.append(response.task)

    return BulkActionResponse(action=action, queued_count=len(tasks), tasks=tasks)
