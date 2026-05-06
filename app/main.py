from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api.assets import router as assets_router
from app.api.auth import router as auth_router
from app.api.overview import router as overview_router
from app.api.settings import router as settings_router
from app.api.tasks import router as tasks_router
from app.api.terminals import router as terminals_router
from app.core.ui import TEMPLATES, build_page_context, page_login_redirect
from app.models.assets import AssetSnapshot, DockerContainerSnapshot
from app.registry.service import RegistryService
from app.services.assets import AssetService
from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import SQLiteTaskStore, TaskStore
from app.tasks.worker import SerialTaskWorker
from app.terminals.system_terminal import SystemTerminalSink


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker = app.state.task_worker
    worker.start()
    try:
        yield
    finally:
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
    task_store: TaskStore | None = None,
) -> FastAPI:
    app = FastAPI(title='WSL Ops Panel', lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(overview_router)
    app.include_router(tasks_router)
    app.include_router(settings_router)
    app.include_router(terminals_router)
    app.include_router(assets_router)
    app.mount('/static', StaticFiles(directory=str(Path(__file__).parent / 'static')), name='static')

    registry_service = RegistryService(Path(config_root))
    asset_service = AssetService(
        registry_service,
        docker_scanner=docker_scanner,
        systemd_scanner=systemd_scanner,
        node_scanner=node_scanner,
        python_scanner=python_scanner,
        host_process_scanner=host_process_scanner,
        system_infra_scanner=system_infra_scanner,
    )
    queue_store = task_store or SQLiteTaskStore()
    task_queue = GlobalTaskQueue(queue_store)
    task_queue.recover_on_startup()
    system_terminal_sink = SystemTerminalSink(Path('data/terminals/system.log'))
    task_worker = SerialTaskWorker(queue=task_queue, sink=system_terminal_sink)

    app.state.registry_service = registry_service
    app.state.asset_service = asset_service
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


app = create_app()
