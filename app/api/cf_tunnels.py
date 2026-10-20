from fastapi import APIRouter, Request

from app.api.assets import get_page_asset

router = APIRouter(prefix="/api/assets", tags=["cf-tunnels"])


@router.get("/{object_id}/cf")
def get_cf_status(object_id: str, request: Request) -> dict[str, object]:
    asset = get_page_asset(request, object_id)
    return request.app.state.cf_service.status(asset)
