from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from app.api.assets import enqueue_asset_action, get_page_asset
from app.core.security import require_authenticated_request
from app.services.notifications import NotificationPreview, ProjectNotificationConfig

router = APIRouter(prefix='/api/notifications', tags=['notifications'])


class NotificationConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    enabled: bool
    title: str
    template: str


class NotificationConfigResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    asset_id: str
    config: ProjectNotificationConfig
    preview: NotificationPreview


@router.get('/{object_id}', response_model=NotificationConfigResponse)
def get_notification_config(object_id: str, request: Request) -> NotificationConfigResponse:
    require_authenticated_request(request)
    asset = get_page_asset(request, object_id)
    service = request.app.state.notification_service
    return NotificationConfigResponse(asset_id=object_id, config=service.get_project_config(asset), preview=service.preview(asset))


@router.post('/{object_id}', response_model=NotificationConfigResponse)
def save_notification_config(object_id: str, payload: NotificationConfigRequest, request: Request) -> NotificationConfigResponse:
    require_authenticated_request(request)
    asset = get_page_asset(request, object_id)
    service = request.app.state.notification_service
    config = service.save_project_config(object_id, enabled=payload.enabled, title=payload.title, template=payload.template)
    return NotificationConfigResponse(asset_id=object_id, config=config, preview=service.preview(asset))


@router.post('/{object_id}/send', status_code=202)
def send_notification(object_id: str, request: Request):
    require_authenticated_request(request)
    asset = get_page_asset(request, object_id)
    if 'notify_send' not in asset.supports_actions:
        raise HTTPException(status_code=400, detail=f'action notify_send is not supported by {object_id}')
    return enqueue_asset_action(request, object_id, action='notify_send')
