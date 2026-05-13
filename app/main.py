from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.assets import router as assets_router
from app.api.auth import router as auth_router
from app.api.bulk_actions import router as bulk_actions_router
from app.api.overview import router as overview_router
from app.api.settings import router as settings_router
from app.api.tasks import router as tasks_router
from app.api.terminals import close_all_terminal_sessions, router as terminals_router
from app.api.terminals import system_terminal_sink
from app.core.ui import TEMPLATES, build_page_context, page_login_redirect
from app.models.assets import AssetSnapshot, DockerContainerSnapshot
from app.models.registry import RegistrySnapshot
from app.recipes.service import DockerRecipeService
from app.registry.service import RegistryService
from app.services.assets import AssetService
from app.services.docker_versions import DockerVersionService
from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import SQLiteTaskStore, TaskStore
from app.tasks.worker import SerialTaskWorker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker = app.state.task_worker
    worker.start()
    try:
        yield
    finally:
        close_all_terminal_sessions()
        worker.stop()


def create_app(
    *,
    config_root: Path | str = Path('config'),
    docker_scanner: Callable[[], list[DockerContainerSnapshot]] | None = None,
    systemd_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    node_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    python_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    host_process_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    system_infra_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    project_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    task_store: TaskStore | None = None,
) -> FastAPI:
    app = FastAPI(title='WSL Ops Panel', lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(bulk_actions_router)
    app.include_router(overview_router)
    app.include_router(tasks_router)
    app.include_router(settings_router)
    app.include_router(terminals_router)
    app.include_router(assets_router)
    app.mount('/static', StaticFiles(directory=str(Path(__file__).parent / 'static')), name='static')

    config_root = Path(config_root)
    docker_version_service = DockerVersionService()
    runtime_services = _build_runtime_services(
        config_root,
        docker_scanner=docker_scanner,
        systemd_scanner=systemd_scanner,
        node_scanner=node_scanner,
        python_scanner=python_scanner,
        host_process_scanner=host_process_scanner,
        system_infra_scanner=system_infra_scanner,
        project_scanner=project_scanner,
        docker_version_service=docker_version_service,
    )
    registry_service = runtime_services.registry_service
    docker_recipe_service = runtime_services.docker_recipe_service
    asset_service = runtime_services.asset_service
    queue_store = task_store or SQLiteTaskStore()
    task_queue = GlobalTaskQueue(queue_store)
    task_queue.recover_on_startup()
    task_worker = SerialTaskWorker(queue=task_queue, sink=system_terminal_sink)

    app.state.config_root = config_root
    app.state.docker_scanner = docker_scanner
    app.state.systemd_scanner = systemd_scanner
    app.state.node_scanner = node_scanner
    app.state.python_scanner = python_scanner
    app.state.host_process_scanner = host_process_scanner
    app.state.system_infra_scanner = system_infra_scanner
    app.state.project_scanner = project_scanner
    app.state.registry_service = registry_service
    app.state.asset_service = asset_service
    app.state.docker_recipe_service = docker_recipe_service
    app.state.docker_version_service = docker_version_service
    app.state.rebuild_registry_runtime = lambda: _rebuild_registry_runtime(app)
    app.state.task_store = queue_store
    app.state.task_queue = task_queue
    app.state.task_worker = task_worker
    app.state.system_terminal_sink = system_terminal_sink

    @app.api_route('/healthz', methods=['GET', 'HEAD'])
    def healthcheck() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/login', response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, 'login.html', {'title': '登录'})

    @app.get('/terminals', response_class=HTMLResponse, name='terminals_page')
    def terminals_page(request: Request) -> HTMLResponse:
        redirect = page_login_redirect(request)
        if redirect is not None:
            return redirect
        context = build_page_context(request, title='终端中心', active_page='terminals')
        return TEMPLATES.TemplateResponse(request, 'terminals.html', context)

    return app


def _build_docker_recipe_service(config_root: Path, registry_snapshot: RegistrySnapshot) -> DockerRecipeService | None:
    recipe_ids = _collect_referenced_recipe_ids(registry_snapshot)
    if not recipe_ids:
        return None
    return DockerRecipeService(config_root, recipe_ids=recipe_ids)


def _collect_referenced_recipe_ids(registry_snapshot: RegistrySnapshot) -> set[str]:
    return {
        obj.config['recipe_id']
        for obj in registry_snapshot.objects
        if obj.type == 'docker_compose' and bool(obj.config.get('recipe_id'))
    }


class RuntimeServices:
    def __init__(
        self,
        registry_service: RegistryService,
        docker_recipe_service: DockerRecipeService | None,
        asset_service: AssetService,
    ) -> None:
        self.registry_service = registry_service
        self.docker_recipe_service = docker_recipe_service
        self.asset_service = asset_service


def _build_runtime_services(
    config_root: Path,
    *,
    docker_scanner: Callable[[], list[DockerContainerSnapshot]] | None = None,
    systemd_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    node_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    python_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    host_process_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    system_infra_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    project_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    docker_version_service: DockerVersionService | None = None,
) -> RuntimeServices:
    registry_service = RegistryService(config_root)
    docker_recipe_service = _build_docker_recipe_service(config_root, registry_service.snapshot)
    asset_service = AssetService(
        registry_service,
        docker_scanner=docker_scanner,
        systemd_scanner=systemd_scanner,
        node_scanner=node_scanner,
        python_scanner=python_scanner,
        host_process_scanner=host_process_scanner,
        system_infra_scanner=system_infra_scanner,
        project_scanner=project_scanner,
        docker_recipe_service=docker_recipe_service,
        docker_version_service=docker_version_service,
        config_root=config_root,
    )
    return RuntimeServices(registry_service, docker_recipe_service, asset_service)


def _rebuild_registry_runtime(app: FastAPI) -> RegistrySnapshot:
    runtime_services = _build_runtime_services(
        app.state.config_root,
        docker_scanner=app.state.docker_scanner,
        systemd_scanner=app.state.systemd_scanner,
        node_scanner=app.state.node_scanner,
        python_scanner=app.state.python_scanner,
        host_process_scanner=app.state.host_process_scanner,
        system_infra_scanner=app.state.system_infra_scanner,
        project_scanner=app.state.project_scanner,
        docker_version_service=app.state.docker_version_service,
    )
    app.state.registry_service = runtime_services.registry_service
    app.state.docker_recipe_service = runtime_services.docker_recipe_service
    app.state.asset_service = runtime_services.asset_service
    return runtime_services.registry_service.snapshot


app = create_app()
