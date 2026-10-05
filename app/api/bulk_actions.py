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


class BulkSkippedItem(BaseModel):
    model_config = ConfigDict(extra='forbid')

    asset_id: str
    reason: str


class BulkActionResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    action: str
    queued_count: int
    tasks: list[TaskRecord]
    skipped: list[BulkSkippedItem] = Field(default_factory=list)


@router.post('/{action_slug}', status_code=202, response_model=BulkActionResponse)
def queue_bulk_action(action_slug: str, payload: BulkActionRequest, request: Request) -> BulkActionResponse:
    require_authenticated_request(request)
    action = ACTION_MAP.get(action_slug)
    if action is None:
        raise HTTPException(status_code=404, detail='bulk action not found')

    tasks: list[TaskRecord] = []
    skipped: list[BulkSkippedItem] = []
    errors: dict[str, str] = {}
    assets = request.app.state.asset_service.get_assets(payload.asset_ids, errors=errors)
    for asset_id in dict.fromkeys(payload.asset_ids):
        try:
            if asset_id in errors:
                skipped.append(BulkSkippedItem(asset_id=asset_id, reason=errors[asset_id]))
                continue
            asset = assets.get(asset_id)
            if asset is None:
                raise HTTPException(status_code=404, detail='asset not found; refresh and retry')
            if action not in asset.supports_actions:
                skipped.append(BulkSkippedItem(asset_id=asset_id, reason=f'action {action} is not supported by {asset_id}'))
                continue
            version = payload.version_map.get(asset_id) if action == 'deploy_version' else None
            if action == 'deploy_version' and not version:
                skipped.append(BulkSkippedItem(asset_id=asset_id, reason=f'version is required for {asset_id}'))
                continue
            response = enqueue_asset_action(request, asset_id, action=action, version=version, asset=asset, render_flash=False)
            if not isinstance(response, AssetActionResponse):
                skipped.append(BulkSkippedItem(asset_id=asset_id, reason='unexpected action response'))
                continue
            tasks.append(response.task)
        except HTTPException as exc:
            skipped.append(BulkSkippedItem(asset_id=asset_id, reason=str(exc.detail)))
        except Exception as exc:
            skipped.append(BulkSkippedItem(asset_id=asset_id, reason=str(exc)))

    return BulkActionResponse(action=action, queued_count=len(tasks), tasks=tasks, skipped=skipped)
