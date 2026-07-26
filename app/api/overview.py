from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.assets import get_page_asset
from app.core.ui import TEMPLATES, build_page_context, page_login_redirect
from app.services.capabilities import get_actions_for_assets
from app.services.agent_workbench import build_agent_workbench_context
from app.services.config_sync import build_config_modules
from app.services.package_catalog import build_package_catalog
from app.services.system_overview import build_system_overview

router = APIRouter(tags=['overview'])


@router.get('/', response_class=HTMLResponse)
def overview_page(request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    nav_categories = build_page_context(request, title='WSL Ops Panel', active_page='overview')['nav_categories']
    first_category = nav_categories[0] if nav_categories else None
    assets = request.app.state.asset_service.list_assets(first_category.id) if first_category is not None else []
    context = build_page_context(
        request,
        title='WSL Ops Panel',
        active_page='overview',
        heading='总览',
        selected_category=first_category,
        assets=assets,
        all_categories=nav_categories,
        task_count=len(request.app.state.task_store.list_all()),
        system_overview=build_system_overview(),
    )
    return TEMPLATES.TemplateResponse(request, 'overview.html', context)


@router.get('/categories/{category_id}', response_class=HTMLResponse, name='category_page')
def category_page(category_id: str, request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    registry_service = request.app.state.registry_service
    category = _get_category_definition(registry_service.snapshot.categories, category_id)
    assets = request.app.state.asset_service.list_assets(category_id)
    context = build_page_context(
        request,
        title=f'Category · {category.label}',
        active_page=category.id,
        heading=category.label,
        selected_category=category,
        assets=assets,
        available_actions=get_actions_for_assets(category_id, assets),
    )
    if category.id in {'remote', 'node', 'python', 'agent'}:
        context['remote_nodes'] = request.app.state.remote_node_store.list_nodes()
    if category.id in {'node', 'python'}:
        package_catalog = build_package_catalog(category.id, assets, config_root=request.app.state.config_root)
        context['package_catalog'] = package_catalog['items']
        context['package_suggestions'] = package_catalog['suggestions']
    if category.id == 'remote':
        context['config_modules'] = build_config_modules()
        return TEMPLATES.TemplateResponse(request, 'remote_category.html', context)
    if category.id == 'agent':
        context.update(build_agent_workbench_context(request.app.state.config_root))
        return TEMPLATES.TemplateResponse(request, 'agent_category.html', context)
    return TEMPLATES.TemplateResponse(request, 'category.html', context)


@router.get('/assets/{object_id}', response_class=HTMLResponse, name='asset_detail')
def asset_detail_page(object_id: str, request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    asset = get_page_asset(request, object_id)
    context = build_page_context(
        request,
        title=asset.name,
        active_page=asset.category,
        asset=asset,
    )
    return TEMPLATES.TemplateResponse(request, 'asset_detail.html', context)


def _get_category_definition(categories, category_id: str):
    for category in categories:
        if category.id == category_id:
            return category
    raise HTTPException(status_code=404, detail='category not found')
