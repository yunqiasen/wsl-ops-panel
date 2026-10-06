from __future__ import annotations

import subprocess
import base64
import shlex

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import ActionPlan
from app.core.security import require_authenticated_request
from app.core.ui import TEMPLATES, build_page_context, page_login_redirect
from app.models.remote_nodes import RemoteNode, RemoteNodeCreate, RemoteNodePublic
from app.services.config_sync import (
    config_apply_command,
    get_config_module,
    is_config_module_compatible,
    read_config_draft,
    read_config_module_text,
    remote_config_scan_command,
    save_config_draft,
)
from app.services.asset_policies import AssetPolicyService
from app.services.package_catalog import parse_install_request, record_package_history
from app.services.package_versions import PackageVersionService

router = APIRouter(tags=["remote-nodes"])


class RemoteNodeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[RemoteNodePublic]


class RemoteNodeTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node: RemoteNodePublic
    ok: bool
    message: str


class RemoteBulkPackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_type: str
    package_action: str
    package_names: list[str]
    version_map: dict[str, str] = Field(default_factory=dict)
    node_ids: list[str]


class RemoteBulkAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_clients: list[str]
    node_ids: list[str]


class RemoteQueuedResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queued_count: int
    tasks: list[str]
    skipped: list[str] = Field(default_factory=list)


class PackageInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_type: str
    package_action: str = "install_or_update"
    package_names: list[str] = Field(default_factory=list)
    install_command: str = ""
    version_map: dict[str, str] = Field(default_factory=dict)
    node_ids: list[str]


class PackageStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_type: str
    package_names: list[str]
    node_ids: list[str]


class PackageStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_name: str
    node_id: str
    status: str
    message: str | None = None


class PackageVersionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_name: str
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = "ok"
    error: str | None = None


class PackageStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PackageStatusItem]


@router.get("/remote/nodes", response_class=HTMLResponse, name="remote_nodes_page")
def remote_nodes_page(request: Request) -> RedirectResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect
    return RedirectResponse(url="/categories/remote", status_code=302)


@router.get(
    "/remote/config-sync", response_class=HTMLResponse, name="remote_config_sync_page"
)
def remote_config_sync_page(request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect
    context = build_page_context(
        request,
        title="配置同步中心",
        active_page="remote",
        heading="配置同步中心",
    )
    return TEMPLATES.TemplateResponse(request, "remote_config_sync.html", context)


@router.post("/remote/nodes", response_class=HTMLResponse)
def create_remote_node_form(
    request: Request,
    name: str = Form(...),
    host: str = Form(...),
    port: int = Form(22),
    username: str = Form(...),
    auth_type: str = Form("password"),
    password: str = Form(""),
    key_path: str = Form(""),
    os_hint: str = Form("auto"),
    tags: str = Form(""),
):
    require_authenticated_request(request)
    request.app.state.remote_node_store.add_node(
        RemoteNodeCreate(
            name=name.strip(),
            host=host.strip(),
            port=port,
            username=username.strip(),
            auth_type=auth_type,
            password=password or None,
            key_path=key_path or None,
            os_hint=os_hint or "auto",
            tags=[item.strip() for item in tags.split(",") if item.strip()],
        )
    )
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.post("/remote/nodes/{node_id}/delete", response_class=HTMLResponse)
def delete_remote_node_form(node_id: str, request: Request):
    require_authenticated_request(request)
    request.app.state.remote_node_store.delete_node(node_id)
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.post("/remote/nodes/{node_id}/test", response_class=HTMLResponse)
def test_remote_node_form(node_id: str, request: Request):
    require_authenticated_request(request)
    node = request.app.state.remote_node_store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="remote node not found")
    ok, message = request.app.state.remote_ssh_service.test_connection(node)
    request.app.state.remote_node_store.update_node_status(
        node_id, status="online" if ok else "error", error=None if ok else message
    )
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.get("/api/remote/nodes", response_model=RemoteNodeListResponse)
def api_list_remote_nodes(request: Request) -> RemoteNodeListResponse:
    require_authenticated_request(request)
    return RemoteNodeListResponse(
        nodes=[
            RemoteNodePublic.from_node(node)
            for node in request.app.state.remote_node_store.list_nodes()
        ]
    )


@router.post("/api/remote/nodes", response_model=RemoteNodePublic, status_code=201)
def api_create_remote_node(
    payload: RemoteNodeCreate, request: Request
) -> RemoteNodePublic:
    require_authenticated_request(request)
    node = request.app.state.remote_node_store.add_node(payload)
    return RemoteNodePublic.from_node(node)


@router.delete("/api/remote/nodes/{node_id}", status_code=204)
def api_delete_remote_node(node_id: str, request: Request) -> None:
    require_authenticated_request(request)
    if not request.app.state.remote_node_store.delete_node(node_id):
        raise HTTPException(status_code=404, detail="remote node not found")


@router.post("/api/remote/nodes/{node_id}/test", response_model=RemoteNodeTestResponse)
def api_test_remote_node(node_id: str, request: Request) -> RemoteNodeTestResponse:
    require_authenticated_request(request)
    node = request.app.state.remote_node_store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="remote node not found")
    ok, message = request.app.state.remote_ssh_service.test_connection(node)
    updated = request.app.state.remote_node_store.update_node_status(
        node_id, status="online" if ok else "error", error=None if ok else message
    )
    return RemoteNodeTestResponse(
        node=RemoteNodePublic.from_node(updated or node), ok=ok, message=message
    )


@router.get(
    "/remote/config-modules/{module_id}",
    response_class=HTMLResponse,
    name="remote_config_module_page",
)
def remote_config_module_page(module_id: str, request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect
    module = get_config_module(module_id)
    if module is None:
        raise HTTPException(status_code=404, detail="config module not found")
    current_text, read_error = read_config_module_text(module_id)
    draft_text = read_config_draft(request.app.state.config_root, module_id)
    context = build_page_context(
        request,
        title=f"配置草稿 · {module['label']}",
        active_page="remote",
        heading=module["label"],
        module=module,
        current_text=current_text,
        draft_text=draft_text if draft_text is not None else current_text,
        has_draft=draft_text is not None,
        read_error=read_error,
    )
    return TEMPLATES.TemplateResponse(request, "remote_config_module.html", context)


@router.post("/remote/config-modules/{module_id}", response_class=HTMLResponse)
def save_remote_config_module_draft(
    module_id: str, request: Request, draft_text: str = Form("")
) -> RedirectResponse:
    require_authenticated_request(request)
    if get_config_module(module_id) is None:
        raise HTTPException(status_code=404, detail="config module not found")
    save_config_draft(request.app.state.config_root, module_id, draft_text)
    request.app.state.system_terminal_sink.write(f"配置草稿已保存：{module_id}\n")
    return RedirectResponse(url=f"/remote/config-modules/{module_id}", status_code=303)


@router.post("/remote/actions/package", response_class=HTMLResponse)
def queue_remote_package_action(
    request: Request,
    tool_type: str = Form(...),
    package_action: str = Form(...),
    package_name: str = Form(""),
    version: str = Form(""),
    node_ids: list[str] = Form(...),
):
    require_authenticated_request(request)
    package_name = package_name.strip()
    if tool_type not in {"node", "python"}:
        raise HTTPException(status_code=400, detail="tool_type must be node or python")
    if package_action not in {"scan", "install_or_update", "delete"}:
        raise HTTPException(status_code=400, detail="unsupported package action")
    if package_action != "scan" and not package_name:
        raise HTTPException(status_code=400, detail="package_name is required")
    policy_service = AssetPolicyService(request.app.state.config_root)
    if package_action != "scan":
        allowed, reason = policy_service.check_package_action(
            tool_type, package_name, package_action, version=version.strip(),
        )
        if not allowed:
            request.app.state.system_terminal_sink.write(
                f"{package_name}: 面板策略 - {reason}，已跳过\n"
            )
            return RedirectResponse(url="/categories/remote", status_code=303)
    tasks = []
    for node_id in node_ids:
        node = _get_remote_node(request, node_id)
        remote_command = _remote_package_command(
            tool_type,
            package_action,
            package_name,
            version.strip(),
            windows=_is_windows_node(node),
        )
        remote_command = _guard_package_shell(policy_service, tool_type, package_action,
                                              [package_name], remote_command, windows=_is_windows_node(node))
        command = request.app.state.remote_ssh_service.build_command(
            node, remote_command
        )
        task = request.app.state.task_queue.enqueue(
            f"remote__{node.id}__{tool_type}",
            f"remote_{tool_type}_{package_action}",
            requested_version=version.strip() or None,
            plan=ActionPlan(commands=[command], command_timeout_seconds=120),
        )
        tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"远程 {tool_type} 任务已入队：{len(tasks)} 个\n"
    )
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.post("/remote/actions/config-scan", response_class=HTMLResponse)
def queue_remote_config_scan(
    request: Request,
    sync_kind: list[str] = Form(...),
    node_ids: list[str] = Form(...),
):
    require_authenticated_request(request)
    module_ids = [item.strip() for item in sync_kind if item.strip()]
    if not module_ids:
        raise HTTPException(status_code=400, detail="sync_kind is required")
    for module_id in module_ids:
        if get_config_module(module_id) is None:
            raise HTTPException(
                status_code=400, detail=f"unknown config module: {module_id}"
            )
    task_count = 0
    for module_id in module_ids:
        for node_id in node_ids:
            if node_id == "__local__":
                remote_command = _remote_config_scan_command(module_id, windows=False)
                command = ["bash", "-lc", remote_command]
                object_id = f"local__config__{module_id}"
            else:
                node = _get_remote_node(request, node_id)
                remote_command = _remote_config_scan_command(
                    module_id, windows=_is_windows_node(node)
                )
                command = request.app.state.remote_ssh_service.build_command(
                    node, remote_command
                )
                object_id = f"remote__{node.id}__config__{module_id}"
            request.app.state.task_queue.enqueue(
                object_id,
                f"remote_config_scan_{module_id}",
                plan=ActionPlan(commands=[command], command_timeout_seconds=90),
            )
            task_count += 1
    request.app.state.system_terminal_sink.write(
        f"配置扫描任务已入队：{len(module_ids)} 个模块 · {len(node_ids)} 个节点 · {task_count} 个任务\n"
    )
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.post("/remote/actions/config-apply", response_class=HTMLResponse)
def queue_remote_config_apply(
    request: Request,
    module_id: str = Form(...),
    operation: str = Form("append_line"),
    content: str = Form(...),
    node_ids: list[str] = Form(...),
):
    require_authenticated_request(request)
    module_id = module_id.strip()
    operation = operation.strip()
    content = content.strip("\r\n")
    if get_config_module(module_id) is None:
        raise HTTPException(
            status_code=400, detail=f"unknown config module: {module_id}"
        )
    if operation != "append_line":
        raise HTTPException(status_code=400, detail="only append_line is supported now")
    if not content.strip():
        raise HTTPException(status_code=400, detail="content is required")

    task_count = 0
    skipped: list[str] = []
    for node_id in node_ids:
        node = None
        if node_id == "__local__":
            os_kind = "wsl"
            windows = False
            node_name = "当前 WSL"
            object_id = f"local__config_apply__{module_id}"
        else:
            node = _get_remote_node(request, node_id)
            windows = _is_windows_node(node)
            os_kind = (
                "windows"
                if windows
                else (
                    node.os_hint
                    if node.os_hint in {"linux", "wsl", "macos"}
                    else "unknown"
                )
            )
            node_name = node.name
            object_id = f"remote__{node.id}__config_apply__{module_id}"
        if not is_config_module_compatible(module_id, os_kind):
            skipped.append(f"{node_name}: 不兼容 {module_id}")
            continue
        remote_command = config_apply_command(
            module_id, operation, content, windows=windows
        )
        if not remote_command:
            skipped.append(f"{node_name}: 暂不支持该配置写入")
            continue
        request.app.state.task_queue.enqueue(
            object_id,
            f"config_apply_{module_id}_{operation}",
            plan=ActionPlan(
                commands=[
                    ["bash", "-lc", remote_command]
                    if node is None
                    else request.app.state.remote_ssh_service.build_command(
                        node, remote_command
                    )
                ],
                command_timeout_seconds=90,
            ),
        )
        task_count += 1
    request.app.state.system_terminal_sink.write(
        f"配置应用任务已入队：{module_id} · {task_count} 个任务，跳过 {len(skipped)} 个：{'; '.join(skipped)}\n"
    )
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.post("/remote/actions/agent-scan", response_class=HTMLResponse)
def queue_remote_agent_scan(
    request: Request,
    agent_client: str = Form("all"),
    node_ids: list[str] = Form(...),
):
    require_authenticated_request(request)
    for node_id in node_ids:
        node = _get_remote_node(request, node_id)
        remote_command = _remote_agent_scan_command(
            agent_client, windows=_is_windows_node(node)
        )
        command = request.app.state.remote_ssh_service.build_command(
            node, remote_command
        )
        request.app.state.task_queue.enqueue(
            f"remote__{node.id}__agent",
            f"remote_agent_scan_{agent_client}",
            plan=ActionPlan(commands=[command], command_timeout_seconds=90),
        )
    request.app.state.system_terminal_sink.write(
        f"Agent 扫描任务已入队：{agent_client} · {len(node_ids)} 个节点\n"
    )
    return RedirectResponse(url="/categories/remote", status_code=303)


@router.get("/api/packages/versions", response_model=PackageVersionsResponse)
def api_package_versions(
    request: Request,
    tool_type: str = Query(...),
    package_name: str = Query(...),
) -> PackageVersionsResponse:
    require_authenticated_request(request)
    package_name = package_name.strip()
    if tool_type not in {"node", "python"}:
        raise HTTPException(status_code=400, detail="tool_type must be node or python")
    if not package_name:
        raise HTTPException(status_code=400, detail="package_name is required")
    service = PackageVersionService()
    info = (
        service.get_node_version_info(package_name)
        if tool_type == "node"
        else service.get_python_version_info(package_name)
    )
    versions = (
        list(reversed(info.versions[-80:]))
        if tool_type == "python"
        else list(reversed(info.versions[-80:]))
    )
    if info.latest_version and info.latest_version not in versions:
        versions.insert(0, info.latest_version)
    return PackageVersionsResponse(
        package_name=package_name,
        latest_version=info.latest_version,
        versions=versions,
        source_status=info.source_status,
        error=info.error,
    )


@router.post(
    "/api/packages/install", response_model=RemoteQueuedResponse, status_code=202
)
def api_queue_package_install(
    payload: PackageInstallRequest, request: Request
) -> RemoteQueuedResponse:
    require_authenticated_request(request)
    if payload.tool_type not in {"node", "python"}:
        raise HTTPException(status_code=400, detail="tool_type must be node or python")
    if payload.package_action not in {"install_or_update", "delete"}:
        raise HTTPException(status_code=400, detail="unsupported package action")
    package_names, parsed_version_map, install_command = parse_install_request(
        payload.tool_type,
        payload.install_command,
        payload.package_names,
    )
    version_map = {
        **parsed_version_map,
        **{
            key: value.strip()
            for key, value in payload.version_map.items()
            if value.strip()
        },
    }
    if not package_names:
        raise HTTPException(
            status_code=400, detail="package_names or install_command is required"
        )
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")

    policy_service = AssetPolicyService(request.app.state.config_root)
    tasks: list[str] = []
    skipped: list[str] = []
    if install_command and payload.package_action == "install_or_update":
        # custom_command: check each parsed package name against the same policy
        blocked_packages: list[tuple[str, str | None]] = []
        for pkg in package_names:
            ok, reason = policy_service.check_package_action(
                payload.tool_type, pkg, payload.package_action, version=version_map.get(pkg, ""),
            )
            if not ok:
                blocked_packages.append((pkg, reason))
        if blocked_packages:
            for pkg, reason in blocked_packages:
                skipped.append(f"{pkg}: 面板策略 - {reason}")
        else:
            for node_id in payload.node_ids:
                task_label = _safe_task_suffix("-".join(package_names[:2]))
                if node_id == "__local__":
                    command = ["bash", "-lc", _guard_package_shell(policy_service, payload.tool_type, payload.package_action,
                                                            package_names, install_command, pip_only=True)]
                    object_id = f"local__{payload.tool_type}__{task_label}"
                else:
                    node = _get_remote_node(request, node_id)
                    if node.status != "online":
                        skipped.append(
                            f"{node.name}: SSH 未连接，跳过 {', '.join(package_names)}"
                        )
                        continue
                    command = request.app.state.remote_ssh_service.build_command(
                        node, _guard_package_shell(policy_service, payload.tool_type, payload.package_action,
                                                   package_names, install_command, windows=_is_windows_node(node), pip_only=True)
                    )
                    object_id = f"remote__{node.id}__{payload.tool_type}__{task_label}"
                task = request.app.state.task_queue.enqueue(
                    object_id,
                    f"{payload.tool_type}_{payload.package_action}",
                    requested_version=",".join(version_map.values()) or None,
                    plan=ActionPlan(commands=[command], command_timeout_seconds=180),
                )
                tasks.append(task.id)
    else:
        # Pre-check all packages against policy (per-package, not per-node)
        allowed_packages: list[str] = []
        for package_name in package_names:
            ok, reason = policy_service.check_package_action(
                payload.tool_type, package_name, payload.package_action, version=version_map.get(package_name, ""),
            )
            if not ok:
                skipped.append(f"{package_name}: 面板策略 - {reason}")
            else:
                allowed_packages.append(package_name)
        for node_id in payload.node_ids:
            for package_name in allowed_packages:
                version = version_map.get(package_name, "").strip()
                if node_id == "__local__":
                    command = _local_package_command(
                        payload.tool_type, payload.package_action, package_name, version
                    )
                    object_id = (
                        f"local__{payload.tool_type}__{_safe_task_suffix(package_name)}"
                    )
                else:
                    node = _get_remote_node(request, node_id)
                    if node.status != "online":
                        skipped.append(f"{node.name}: SSH 未连接，跳过 {package_name}")
                        continue
                    remote_command = _remote_package_command(
                        payload.tool_type,
                        payload.package_action,
                        package_name,
                        version,
                        windows=_is_windows_node(node),
                    )
                    remote_command = _guard_package_shell(policy_service, payload.tool_type, payload.package_action,
                                                          [package_name], remote_command, windows=_is_windows_node(node))
                    command = request.app.state.remote_ssh_service.build_command(
                        node, remote_command
                    )
                    object_id = f"remote__{node.id}__{payload.tool_type}__{_safe_task_suffix(package_name)}"
                task = request.app.state.task_queue.enqueue(
                    object_id,
                    f"{payload.tool_type}_{payload.package_action}",
                    requested_version=version or None,
                    plan=ActionPlan(commands=([_python_absence_command([package_name], interpreter=command[0])]
                                               if node_id == "__local__" and policy_service.install_only(payload.tool_type, package_name, payload.package_action)
                                               else []) + [command], command_timeout_seconds=180),
                )
                tasks.append(task.id)
    if tasks and payload.package_action == "install_or_update":
        record_package_history(
            request.app.state.config_root,
            payload.tool_type,
            package_names,
            install_command,
        )
    action_label = (
        "安装/更新" if payload.package_action == "install_or_update" else "删除"
    )
    request.app.state.system_terminal_sink.write(
        f"{_tool_type_label(payload.tool_type)} 工具{action_label}已入队：共 {len(payload.node_ids)} 台设备，创建 {len(tasks)} 个任务，跳过 {len(skipped)} 个\n"
    )
    return RemoteQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


@router.post("/api/packages/status", response_model=PackageStatusResponse)
def api_package_status(
    payload: PackageStatusRequest, request: Request
) -> PackageStatusResponse:
    require_authenticated_request(request)
    if payload.tool_type not in {"node", "python"}:
        raise HTTPException(status_code=400, detail="tool_type must be node or python")
    package_names = [name.strip() for name in payload.package_names if name.strip()]
    if not package_names:
        raise HTTPException(status_code=400, detail="package_names is required")

    items: list[PackageStatusItem] = []
    for node_id in payload.node_ids:
        for package_name in package_names:
            if node_id == "__local__":
                status, message = _check_local_package_status(
                    payload.tool_type, package_name
                )
            else:
                node = _get_remote_node(request, node_id)
                if node.status != "online":
                    status, message = "unavailable", node.last_error or "SSH 未连接"
                else:
                    status, message = _check_remote_package_status(
                        request, node, payload.tool_type, package_name
                    )
            items.append(
                PackageStatusItem(
                    package_name=package_name,
                    node_id=node_id,
                    status=status,
                    message=message,
                )
            )
    return PackageStatusResponse(items=items)


@router.post(
    "/api/remote/actions/package-bulk",
    response_model=RemoteQueuedResponse,
    status_code=202,
)
def api_queue_remote_package_bulk(
    payload: RemoteBulkPackageRequest, request: Request
) -> RemoteQueuedResponse:
    require_authenticated_request(request)
    if payload.tool_type not in {"node", "python"}:
        raise HTTPException(status_code=400, detail="tool_type must be node or python")
    if payload.package_action not in {"install_or_update", "delete"}:
        raise HTTPException(status_code=400, detail="unsupported package action")
    package_names = [name.strip() for name in payload.package_names if name.strip()]
    if not package_names:
        raise HTTPException(status_code=400, detail="package_names is required")
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")

    policy_service = AssetPolicyService(request.app.state.config_root)
    tasks: list[str] = []
    skipped: list[str] = []
    # Pre-check all packages against policy (per-package, not per-node)
    allowed_packages: list[str] = []
    for package_name in package_names:
        ok, reason = policy_service.check_package_action(
            payload.tool_type, package_name, payload.package_action, version=payload.version_map.get(package_name, ""),
        )
        if not ok:
            skipped.append(f"{package_name}: 面板策略 - {reason}")
        else:
            allowed_packages.append(package_name)
    for node_id in payload.node_ids:
        node = _get_remote_node(request, node_id)
        if node.status != "online":
            skipped.extend(
                f"{node.name}: SSH 未连接，跳过 {package_name}"
                for package_name in allowed_packages
            )
            continue
        for package_name in allowed_packages:
            version = payload.version_map.get(package_name, "").strip()
            remote_command = _remote_package_command(
                payload.tool_type,
                payload.package_action,
                package_name,
                version,
                windows=_is_windows_node(node),
            )
            remote_command = _guard_package_shell(policy_service, payload.tool_type, payload.package_action,
                                                  [package_name], remote_command, windows=_is_windows_node(node))
            command = request.app.state.remote_ssh_service.build_command(
                node, remote_command
            )
            task = request.app.state.task_queue.enqueue(
                f"remote__{node.id}__{payload.tool_type}__{_safe_task_suffix(package_name)}",
                f"remote_{payload.tool_type}_{payload.package_action}",
                requested_version=version or None,
                plan=ActionPlan(commands=[command], command_timeout_seconds=180),
            )
            tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"远程 {_tool_type_label(payload.tool_type)} 包任务已入队：创建 {len(tasks)} 个任务，跳过 {len(skipped)} 个\n"
    )
    return RemoteQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


@router.post(
    "/api/remote/actions/agent-bulk",
    response_model=RemoteQueuedResponse,
    status_code=202,
)
def api_queue_remote_agent_bulk(
    payload: RemoteBulkAgentRequest, request: Request
) -> RemoteQueuedResponse:
    require_authenticated_request(request)
    clients = [client.strip() for client in payload.agent_clients if client.strip()]
    if not clients:
        raise HTTPException(status_code=400, detail="agent_clients is required")
    if not payload.node_ids:
        raise HTTPException(status_code=400, detail="node_ids is required")

    tasks: list[str] = []
    skipped: list[str] = []
    for node_id in payload.node_ids:
        for client in clients:
            if node_id == "__local__":
                remote_command = _remote_agent_scan_command(client, windows=False)
                command = ["bash", "-lc", remote_command]
                object_id = f"agent__local__{client}"
            else:
                node = _get_remote_node(request, node_id)
                if node.status != "online":
                    skipped.append(f"{node.name}: SSH 未连接，跳过 {client}")
                    continue
                remote_command = _remote_agent_scan_command(
                    client, windows=_is_windows_node(node)
                )
                command = request.app.state.remote_ssh_service.build_command(
                    node, remote_command
                )
                object_id = f"remote__{node.id}__agent__{client}"
            task = request.app.state.task_queue.enqueue(
                object_id,
                f"agent_config_scan_{client}",
                plan=ActionPlan(commands=[command], command_timeout_seconds=120),
            )
            tasks.append(task.id)
    request.app.state.system_terminal_sink.write(
        f"Agent 配置扫描任务已入队：{len(tasks)} 个\n"
    )
    return RemoteQueuedResponse(queued_count=len(tasks), tasks=tasks, skipped=skipped)


def _python_absence_command(names: list[str], *, interpreter: str = 'python3',
                            pip_launcher: str | None = None) -> list[str]:
    if pip_launcher:
        # pip may belong to a different environment from the guard's Python.
        script = "import json,re,subprocess\n"
        script += f"result = subprocess.run([{pip_launcher!r}, 'list', '--format=json'], capture_output=True, text=True, check=True)\n"
        script += "normalize = lambda name: re.sub(r'[-_.]+', '-', name).lower()\n"
        script += "installed = {normalize(p['name']) for p in json.loads(result.stdout)}\n"
        script += f"for name in {names!r}:\n if normalize(name) in installed: raise SystemExit('白名单外包已安装，跳过更新：' + name)\n"
    else:
        script = "import importlib.metadata as m\n"
        script += f"for name in {names!r}:\n"
        script += " try: m.version(name)\n except m.PackageNotFoundError: continue\n raise SystemExit('白名单外包已安装，跳过更新：' + name)\n"
    encoded = base64.b64encode(script.encode()).decode('ascii')
    return [interpreter, '-c', f"import base64;exec(base64.b64decode('{encoded}'))"]


def _guard_package_shell(policy: AssetPolicyService, tool_type: str, action: str,
                         names: list[str], command: str, *, windows: bool = False,
                         pip_only: bool = False) -> str:
    guarded = [name for name in names if policy.install_only(tool_type, name, action)]
    if not guarded:
        return command
    interpreter = 'python' if windows else 'python3'
    pip_launcher = None
    if pip_only:
        try:
            words = shlex.split(command)
        except ValueError:
            return command
        if not words or not (words[0] in {'pip', 'pip3'} or words[:3] in
                              [['python', '-m', 'pip'], ['python3', '-m', 'pip']]):
            return command
        if words[:3] in [['python', '-m', 'pip'], ['python3', '-m', 'pip']]:
            interpreter = words[0]
        else:
            pip_launcher = words[0]
    guard = _python_absence_command(guarded, interpreter=interpreter, pip_launcher=pip_launcher)
    prefix = ' '.join(_cmd_arg(word, windows=windows) for word in guard)
    return f'{prefix} && {command}'


def _tool_type_label(tool_type: str) -> str:
    return {"node": "Node", "python": "Python"}.get(tool_type, tool_type)


def _get_remote_node(request: Request, node_id: str) -> RemoteNode:
    node = request.app.state.remote_node_store.get_node(node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"remote node not found: {node_id}")
    return node


def _local_package_command(
    tool_type: str, action: str, package_name: str, version: str
) -> list[str]:
    if tool_type == "node":
        target = package_name if not version else f"{package_name}@{version}"
        if action == "install_or_update":
            return ["npm", "install", "-g", target]
        return ["npm", "uninstall", "-g", package_name]
    target = package_name if not version else f"{package_name}=={version}"
    if action == "install_or_update":
        return ["python3", "-m", "pip", "install", "-U", target]
    return ["python3", "-m", "pip", "uninstall", "-y", package_name]


def _check_local_package_status(
    tool_type: str, package_name: str
) -> tuple[str, str | None]:
    command = _package_status_shell_command(tool_type, package_name, windows=False)
    return _run_status_command(["bash", "-lc", command])


def _check_remote_package_status(
    request: Request, node: RemoteNode, tool_type: str, package_name: str
) -> tuple[str, str | None]:
    remote_command = _package_status_shell_command(
        tool_type, package_name, windows=_is_windows_node(node)
    )
    command = request.app.state.remote_ssh_service.build_command(node, remote_command)
    return _run_status_command(command)


def _run_status_command(command: list[str]) -> tuple[str, str | None]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
        )
    except subprocess.TimeoutExpired:
        return "unknown", "检查超时"
    output = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if "installed" in output.split():
        return "installed", output
    if "missing" in output.split():
        return "missing", output
    return (
        "unknown" if completed.returncode == 0 else "error",
        output or f"exit {completed.returncode}",
    )


def _package_status_shell_command(
    tool_type: str, package_name: str, *, windows: bool
) -> str:
    if tool_type == "node":
        if windows:
            return f"npm list -g {_cmd_arg(package_name, windows=True)} --depth=0 >NUL 2>NUL && echo installed || echo missing"
        return f"npm list -g {_sq(package_name)} --depth=0 >/dev/null 2>&1 && echo installed || echo missing"
    if windows:
        return f"python -m pip show {_cmd_arg(package_name, windows=True)} >NUL 2>NUL && echo installed || echo missing"
    return f"(python3 -m pip show {_sq(package_name)} >/dev/null 2>&1 || python -m pip show {_sq(package_name)} >/dev/null 2>&1) && echo installed || echo missing"


def _remote_package_command(
    tool_type: str,
    action: str,
    package_name: str,
    version: str,
    *,
    windows: bool = False,
) -> str:
    if tool_type == "node":
        if action == "scan":
            return (
                "echo ## Node & node -v & npm -v & npm list -g --depth=0"
                if windows
                else 'set -e; echo "## Node"; node -v 2>/dev/null || true; npm -v 2>/dev/null || true; npm list -g --depth=0 2>/dev/null || true'
            )
        target = package_name if not version else f"{package_name}@{version}"
        if action == "install_or_update":
            return f"npm install -g {_cmd_arg(target, windows=windows)}"
        return f"npm uninstall -g {_cmd_arg(package_name, windows=windows)}"
    if action == "scan":
        return (
            "echo ## Python & python --version & python -m pip list"
            if windows
            else 'set -e; echo "## Python"; python3 --version 2>/dev/null || python --version 2>/dev/null || true; python3 -m pip list 2>/dev/null || python -m pip list 2>/dev/null || true'
        )
    target = package_name if not version else f"{package_name}=={version}"
    if action == "install_or_update":
        return (
            f"python -m pip install -U {_cmd_arg(target, windows=windows)}"
            if windows
            else f"python3 -m pip install -U {_sq(target)}"
        )
    return (
        f"python -m pip uninstall -y {_cmd_arg(package_name, windows=windows)}"
        if windows
        else f"python3 -m pip uninstall -y {_sq(package_name)}"
    )


def _remote_config_scan_command(sync_kind: str, *, windows: bool = False) -> str:
    module_command = remote_config_scan_command(sync_kind, windows=windows)
    if module_command is not None:
        return module_command
    if windows:
        commands = {
            "shell": "echo ## Shell config & if exist %USERPROFILE%\\.bashrc dir %USERPROFILE%\\.bashrc & if exist %USERPROFILE%\\.zshrc dir %USERPROFILE%\\.zshrc",
            "proxy": 'echo ## Proxy env & set | findstr /I "proxy clash mihomo"',
            "git": "echo ## Git global config & git config --global --list",
            "node": 'echo ## Node config & npm config list -l | findstr /I "registry proxy prefix"',
            "python": "echo ## Python config & python -m pip config list",
            "ssh": "echo ## SSH config & if exist %USERPROFILE%\\.ssh dir %USERPROFILE%\\.ssh",
            "env": 'echo ## Env & set | findstr /I "PATH HOME USERPROFILE proxy"',
            "agent": _remote_agent_scan_command("all", windows=True),
        }
        return commands.get(sync_kind, commands["shell"]) + " & exit /b 0"
    commands = {
        "shell": 'echo "## Shell config"; for f in ~/.zshrc ~/.bashrc ~/.profile ~/.zprofile; do [ -f "$f" ] && ls -l "$f"; done; true',
        "proxy": 'echo "## Proxy env"; env | grep -Ei "(^|_)(http|https|all)_?proxy=|clash|mihomo" || true',
        "git": 'echo "## Git global config"; git config --global --list 2>/dev/null || true',
        "node": 'echo "## Node config"; npm config list -l 2>/dev/null | grep -Ei "registry|proxy|prefix" || true',
        "python": 'echo "## Python config"; python3 -m pip config list 2>/dev/null || python -m pip config list 2>/dev/null || true',
        "ssh": 'echo "## SSH config"; for f in ~/.ssh/config ~/.ssh/known_hosts; do [ -f "$f" ] && ls -l "$f"; done; true',
        "env": 'echo "## Env"; env | grep -Ei "^(PATH|SHELL|HOME|USER|http_proxy|https_proxy|all_proxy|HTTP_PROXY|HTTPS_PROXY|ALL_PROXY)=" || true',
        "agent": _remote_agent_scan_command("all", windows=False),
    }
    return commands.get(sync_kind, commands["shell"])


def _remote_agent_scan_command(agent_client: str, *, windows: bool = False) -> str:
    if windows:
        win_paths = {
            "codex": ["%USERPROFILE%\\.codex"],
            "claude": ["%USERPROFILE%\\.claude", "%USERPROFILE%\\.claude.json"],
            "gemini": ["%USERPROFILE%\\.gemini"],
            "opencode": [
                "%USERPROFILE%\\AppData\\Local\\opencode",
                "%USERPROFILE%\\.config\\opencode",
            ],
            "openclaw": ["%USERPROFILE%\\.openclaw"],
            "hermes": ["%USERPROFILE%\\.hermes"],
            "all": [
                "%USERPROFILE%\\.codex",
                "%USERPROFILE%\\.claude",
                "%USERPROFILE%\\.claude.json",
                "%USERPROFILE%\\.gemini",
                "%USERPROFILE%\\AppData\\Local\\opencode",
                "%USERPROFILE%\\.openclaw",
                "%USERPROFILE%\\.hermes",
            ],
        }
        checks = " & ".join(
            f'if exist "{path}" dir "{path}"'
            for path in win_paths.get(agent_client, win_paths["all"])
        )
        return f"echo ## Agent scan: {agent_client} & {checks} & exit /b 0"
    paths = {
        "codex": "~/.codex ~/.codex/skills ~/.codex/config.toml ~/.codex/AGENTS.md",
        "claude": "~/.claude ~/.claude.json ~/.claude/CLAUDE.md",
        "gemini": "~/.gemini ~/.gemini/GEMINI.md",
        "opencode": "~/.config/opencode ~/.local/share/opencode",
        "openclaw": "~/.openclaw",
        "hermes": "~/.hermes",
        "all": "~/.codex ~/.claude ~/.claude.json ~/.gemini ~/.config/opencode ~/.openclaw ~/.hermes",
    }
    selected = paths.get(agent_client, paths["all"])
    return f'echo "## Agent scan: {agent_client}"; for p in {selected}; do eval "x=$p"; [ -e "$x" ] && ls -ld "$x"; done; true'


def _is_windows_node(node: RemoteNode) -> bool:
    return node.os_hint == "windows" or node.username.lower() in {
        "administrator",
        "admin",
    }


def _safe_task_suffix(value: str) -> str:
    return (
        "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
        or "package"
    )


def _cmd_arg(value: str, *, windows: bool) -> str:
    if not windows:
        return _sq(value)
    return '"' + value.replace('"', '\\"') + '"'


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
