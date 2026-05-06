from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.security import is_authenticated_request
from app.models.registry import CategoryDefinition

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / 'templates'))
FIXED_PAGES = [
    {'id': 'tasks', 'label': '任务中心', 'route_name': 'tasks_page'},
    {'id': 'logs', 'label': '日志中心', 'route_name': 'logs_page'},
    {'id': 'terminals', 'label': '终端中心', 'route_name': 'terminals_page'},
    {'id': 'settings', 'label': '设置', 'route_name': 'settings_page'},
]


def page_login_redirect(request: Request) -> RedirectResponse | None:
    if is_authenticated_request(request):
        return None
    return RedirectResponse(url='/login', status_code=302)


def build_page_context(request: Request, *, title: str, active_page: str | None = None, **extra: object) -> dict[str, object]:
    registry_service = request.app.state.registry_service
    return {
        'title': title,
        'nav_categories': list_enabled_categories(registry_service.snapshot.categories),
        'fixed_pages': FIXED_PAGES,
        'active_page': active_page,
        **extra,
    }


def list_enabled_categories(categories: list[CategoryDefinition]) -> list[CategoryDefinition]:
    return sorted((category for category in categories if category.enabled), key=lambda item: item.order)
