from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.security import require_authenticated_request
from app.core.ui import TEMPLATES, build_page_context, page_login_redirect

router = APIRouter(tags=['settings'])


@router.get('/settings', response_class=HTMLResponse, name='settings_page')
def settings_page(request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    snapshot = request.app.state.registry_service.snapshot
    context = build_page_context(
        request,
        title='设置',
        active_page='settings',
        registry_snapshot=snapshot,
        reload_message=None,
    )
    return TEMPLATES.TemplateResponse(request, 'settings.html', context)


@router.post('/settings/reload-registry', name='reload_registry')
def reload_registry(request: Request):
    if request.headers.get('hx-request') == 'true':
        require_authenticated_request(request)
    else:
        redirect = page_login_redirect(request)
        if redirect is not None:
            return redirect

    snapshot = request.app.state.registry_service.reload()
    if request.headers.get('hx-request') == 'true':
        markup = (
            '<div class="flash flash-success">'
            f'注册表已重载：{len(snapshot.categories)} 个分类，{len(snapshot.objects)} 个对象'
            '</div>'
        )
        return HTMLResponse(markup)
    return RedirectResponse(url='/settings', status_code=302)
