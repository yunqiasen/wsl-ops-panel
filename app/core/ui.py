from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.core.security import is_authenticated_request
from app.core.settings import load_panel_config
from app.models.registry import CategoryDefinition

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / 'templates'))
STATIC_ROOT = Path(__file__).resolve().parent.parent / 'static'
FIXED_PAGES = [
    {'id': 'tasks', 'label': '任务中心', 'route_name': 'tasks_page'},
    {'id': 'terminals', 'label': '终端中心', 'route_name': 'terminals_page'},
    {'id': 'settings', 'label': '设置', 'route_name': 'settings_page'},
]


def page_login_redirect(request: Request) -> RedirectResponse | None:
    if is_authenticated_request(request):
        return None
    return RedirectResponse(url='/login', status_code=302)


def build_page_context(request: Request, *, title: str, active_page: str | None = None, **extra: object) -> dict[str, object]:
    registry_service = request.app.state.registry_service
    panel_config = load_panel_config(request.app.state.config_root)
    return {
        'title': title,
        'page_title': f'{panel_config.title_cn} · {title}' if title else panel_config.title_cn,
        'panel_title_cn': panel_config.title_cn,
        'panel_title_en': panel_config.title_en,
        'panel_logo_path': '/static/assets/wsl-ops-logo.png',
        'asset_version': static_asset_version(),
        'nav_categories': list_enabled_categories(registry_service.snapshot.categories),
        'fixed_pages': FIXED_PAGES,
        'active_page': active_page,
        **extra,
    }


def list_enabled_categories(categories: list[CategoryDefinition]) -> list[CategoryDefinition]:
    return sorted((category for category in categories if category.enabled), key=lambda item: item.order)


def static_asset_version() -> str:
    candidates = [
        STATIC_ROOT / 'app.css',
        STATIC_ROOT / 'app.js',
        STATIC_ROOT / 'vendor' / 'xterm' / 'xterm.js',
        STATIC_ROOT / 'vendor' / 'xterm' / 'addon-fit.js',
        STATIC_ROOT / 'vendor' / 'xterm' / 'addon-unicode11.js',
        STATIC_ROOT / 'assets' / 'wsl-ops-logo.png',
    ]
    mtimes = [path.stat().st_mtime_ns for path in candidates if path.exists()]
    return str(max(mtimes)) if mtimes else '1'
