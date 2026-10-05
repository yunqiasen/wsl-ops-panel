from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.agent import router as agent_router
from app.api.agent_router import router as agent_router_control_router
from app.api.assets import router as assets_router
from app.api.auth import router as auth_router
from app.api.bulk_actions import router as bulk_actions_router
from app.api.notifications import router as notifications_router
from app.api.remote_nodes import router as remote_nodes_router
from app.api.overview import router as overview_router
from app.api.settings import router as settings_router
from app.api.tasks import router as tasks_router
from app.api.terminals import close_all_terminal_sessions, router as terminals_router
from app.core.ui import TEMPLATES, build_page_context, page_login_redirect
from app.models.assets import AssetSnapshot, DockerContainerSnapshot
from app.models.registry import RegistrySnapshot
from app.recipes.service import DockerRecipeService
from app.registry.service import RegistryService
from app.services.assets import AssetService
from app.services.docker_versions import DockerVersionService
from app.services.notifications import NotificationService
from app.services.agent_mcp import agent_data_root
from app.services.agent_router_config import AgentRouterConfigStore
from app.services.agent_router_control import AgentRouterController
from app.services.remote_nodes import (
    RemoteNodeStore,
    RemoteSSHService,
    default_remote_nodes_path,
)
from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import SQLiteTaskStore, TaskStore
from app.tasks.worker import SerialTaskWorker
from app.services.state_store import resolve_state_db_path
from app.terminals.debug_terminal import DebugTerminalManager
from app.terminals.system_terminal import SystemTerminalSink


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    worker = app.state.task_worker
    app.state.task_queue.recover_on_startup()
    worker.start()
    try:
        yield
    finally:
        close_all_terminal_sessions(app)
        worker.stop()


def create_app(
    *,
    config_root: Path | str = Path("config"),
    docker_scanner: Callable[[], list[DockerContainerSnapshot]] | None = None,
    systemd_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    node_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    python_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    host_process_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    system_infra_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    project_scanner: Callable[[], list[AssetSnapshot]] | None = None,
    task_store: TaskStore | None = None,
) -> FastAPI:
    app = FastAPI(title="WSL Ops Panel", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(agent_router)
    app.include_router(agent_router_control_router)
    app.include_router(bulk_actions_router)
    app.include_router(overview_router)
    app.include_router(notifications_router)
    app.include_router(remote_nodes_router)
    app.include_router(tasks_router)
    app.include_router(settings_router)
    app.include_router(terminals_router)
    app.include_router(assets_router)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    config_root = Path(config_root).resolve()
    data_root = resolve_state_db_path(config_root).parent
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
    queue_store = task_store if task_store is not None else SQLiteTaskStore(data_root / "tasks.sqlite3")
    task_queue = GlobalTaskQueue(queue_store, operations_root=data_root / "operations")
    system_terminal_sink = SystemTerminalSink(data_root / "terminals/system.log")
    task_worker = SerialTaskWorker(queue=task_queue, sink=system_terminal_sink)

    app.state.config_root = config_root
    app.state.data_root = data_root
    app.state.debug_terminal_manager = DebugTerminalManager(base_dir=data_root / "terminals")
    app.state.terminal_upload_root = data_root / "uploads/terminals"
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
    app.state.notification_service = NotificationService(config_root)
    app.state.agent_router_controller = AgentRouterController(
        AgentRouterConfigStore(agent_data_root(config_root))
    )
    app.state.remote_node_store = RemoteNodeStore(
        default_remote_nodes_path(config_root)
    )
    app.state.remote_ssh_service = RemoteSSHService(
        credential_root=default_remote_nodes_path(config_root).parent / "sshpass"
    )
    app.state.rebuild_registry_runtime = lambda: _rebuild_registry_runtime(app)
    app.state.task_store = queue_store
    app.state.task_queue = task_queue
    app.state.task_worker = task_worker
    app.state.system_terminal_sink = system_terminal_sink

    @app.api_route("/healthz", methods=["GET", "HEAD"])
    def healthcheck() -> JSONResponse:
        healthy = app.state.task_worker.is_alive
        return JSONResponse({"status": "ok" if healthy else "degraded"}, status_code=200 if healthy else 503)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        context = build_page_context(request, title="登录", active_page="login")
        return TEMPLATES.TemplateResponse(request, "login.html", context)

    @app.get("/terminals", response_class=HTMLResponse, name="terminals_page")
    def terminals_page(request: Request) -> HTMLResponse:
        redirect = page_login_redirect(request)
        if redirect is not None:
            return redirect
        context = build_page_context(request, title="终端中心", active_page="terminals")
        return TEMPLATES.TemplateResponse(request, "terminals.html", context)

    return app


def _build_docker_recipe_service(
    config_root: Path, registry_snapshot: RegistrySnapshot
) -> DockerRecipeService | None:
    recipe_ids = _collect_referenced_recipe_ids(registry_snapshot)
    if not recipe_ids:
        return None
    return DockerRecipeService(config_root, recipe_ids=recipe_ids)


def _collect_referenced_recipe_ids(registry_snapshot: RegistrySnapshot) -> set[str]:
    return {
        obj.config["recipe_id"]
        for obj in registry_snapshot.objects
        if obj.type == "docker_compose" and bool(obj.config.get("recipe_id"))
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
    docker_recipe_service = _build_docker_recipe_service(
        config_root, registry_service.snapshot
    )
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
