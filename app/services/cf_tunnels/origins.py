"""Read actual runtime bindings; display links and EXPOSE are not host bindings."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
from typing import Any
from time import monotonic

import httpx

from app.models.assets import AssetSnapshot, DockerContainerSnapshot
from app.services.cf_tunnels.http_probe import probe
from app.services.cf_tunnels.models import CFError
from app.services.cf_tunnels.processes import listeners

_DOCKER_FIELDS = (
    '{"id":{{json .Id}},"name":{{json .Name}},"running":{{json .State.Running}},'
    '"started_at":{{json .State.StartedAt}},"pid":{{json .State.Pid}},"network_mode":{{json .HostConfig.NetworkMode}},'
    '"ports":{{json .NetworkSettings.Ports}},"networks":{{json .NetworkSettings.Networks}},'
    '"labels":{{json .Config.Labels}}}'
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def owner_key(asset: AssetSnapshot) -> str:
    directory = asset.metadata.get("project_dir") or asset.metadata.get("path")
    if (
        asset.category not in {"docker", "project"}
        or not isinstance(directory, str)
        or not directory
    ):
        raise CFError("origin_identity", "项目尚缺少可确认的本地目录归属")
    context = sorted(
        {item.compose_project for item in asset.containers if item.compose_project}
    )
    return digest([asset.category, str(Path(directory).resolve()), context])


def inspect_container(name: str, *, timeout: float = 5) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "container", "inspect", "--format", _DOCKER_FIELDS, name],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise CFError("origin_identity", "Docker 运行信息格式错误")
    return result


def remaining(deadline: float, maximum: float) -> float:
    value = min(maximum, deadline - monotonic())
    if value <= 0:
        raise CFError("origin_probe", "项目入口准备超时")
    return value


def prepare_targets(
    asset: AssetSnapshot,
    transport: httpx.AsyncBaseTransport | None,
    *,
    timeout: float = 15,
    probe_timeout: float = 5,
    proc_root: Path = Path("/proc"),
) -> dict[str, object]:
    deadline = monotonic() + timeout
    key = owner_key(asset)
    if asset.category == "project":
        return {
            "targets": _project_targets(
                asset,
                transport,
                key=key,
                deadline=deadline,
                probe_timeout=probe_timeout,
                proc_root=proc_root,
            ),
            "selected_id": None,
        }
    primary = next(
        (c for c in asset.containers if c.name == asset.primary_container_name), None
    )
    if primary is None:
        raise CFError("origin_identity", "项目尚缺少主容器")
    targets = []
    failures = []
    for container in sorted(
        asset.containers, key=lambda item: item.name != primary.name
    ):
        try:
            found = _container_targets(
                asset,
                container,
                transport,
                key=key,
                expected_project=primary.compose_project,
                deadline=deadline,
                probe_timeout=probe_timeout,
                proc_root=proc_root,
            )
            for target in found:
                existing = next(
                    (item for item in targets if item["id"] == target["id"]), None
                )
                if existing is None:
                    targets.append(target)
                elif not existing["reachable"] and target["reachable"]:
                    targets[targets.index(existing)] = target
        except CFError as exc:
            if exc.stage == "origin_identity":
                raise
            failures.append(exc)
    if failures and not any(target["reachable"] for target in targets):
        raise failures[0]
    return {"targets": targets, "selected_id": None}


def _container_targets(
    asset: AssetSnapshot,
    container: DockerContainerSnapshot,
    transport: httpx.AsyncBaseTransport | None,
    *,
    key: str,
    expected_project: str | None,
    deadline: float,
    probe_timeout: float,
    proc_root: Path,
) -> list[dict]:
    info = inspect_container(container.name, timeout=remaining(deadline, 5))
    labels = info.get("labels") or {}
    expected_dir = str(Path(str(asset.metadata["project_dir"])).resolve())
    observed_dir = labels.get("com.docker.compose.project.working_dir")
    if (
        not str(info.get("id", "")).startswith(container.id)
        or not observed_dir
        or str(Path(observed_dir).resolve()) != expected_dir
        or labels.get("com.docker.compose.project") != expected_project
        or labels.get("com.docker.compose.service") != container.compose_service
    ):
        raise CFError("origin_identity", "容器归属已变化，请刷新项目后重试")
    if not info.get("running"):
        raise CFError("origin_probe", "项目容器尚未运行")
    if info.get("network_mode") == "host":
        processes = subprocess.run(
            ["docker", "top", info["id"], "-eo", "pid"],
            check=True,
            capture_output=True,
            text=True,
            timeout=remaining(deadline, 5),
        )
        pids = {int(value) for value in processes.stdout.split() if value.isdecimal()}
        if not info.get("pid") or info["pid"] not in pids:
            raise CFError("origin_identity", "host 网络容器进程归属待确认")
        result = []
        for listener in listeners(proc_root, timeout=remaining(deadline, 5)):
            if listener["pid"] not in pids:
                continue
            target = _listener_target(
                listener,
                transport,
                key=key,
                role=container.compose_service,
                deadline=deadline,
                probe_timeout=probe_timeout,
                source="host_network",
                instance=[info["id"], info.get("started_at")],
            )
            _merge_target(result, target)
        return result
    targets = []
    failures = []
    for exposed, bindings in (info.get("ports") or {}).items():
        if not exposed.endswith("/tcp"):
            continue
        source = "published_port"
        if not isinstance(bindings, list):
            source = "container_network"
            bindings = [
                {"HostIp": network[field], "HostPort": exposed.split("/", 1)[0]}
                for network in (info.get("networks") or {}).values()
                for field in ("IPAddress", "GlobalIPv6Address")
                if network.get(field) and info.get("network_mode") != "host"
            ]
        for binding in bindings:
            address = ipaddress.ip_address(binding.get("HostIp") or "0.0.0.0")
            host = (
                ("::1" if address.version == 6 else "127.0.0.1")
                if address.is_unspecified
                else str(address)
            )
            port = int(binding["HostPort"])
            if not 1 <= port <= 65535:
                continue
            authority = f"[{host}]" if ":" in host else host
            origin = f"http://{authority}:{port}"
            target_id = digest([key, container.compose_service, exposed, "http"])
            existing = next((item for item in targets if item["id"] == target_id), None)
            if existing and existing["reachable"]:
                continue
            limit = remaining(deadline, probe_timeout)
            try:
                checked = probe(transport, f"{origin}/", timeout=limit)
            except (httpx.HTTPError, CFError) as exc:
                failure = (
                    exc
                    if isinstance(exc, CFError)
                    else CFError(
                        "origin_probe", f"项目入口读取失败 ({type(exc).__name__})"
                    )
                )
                failures.append(failure)
                checked = {
                    "reachable": False,
                    "status_code": None,
                    "fingerprint": None,
                    "reason": str(failure),
                }
            target = {
                "id": target_id,
                "revision": digest([key, info["id"], info.get("started_at"), origin]),
                "name": f"{container.compose_service} · {exposed}",
                "origin": origin,
                "path": "/",
                "source": source,
                **checked,
            }
            if existing is None:
                targets.append(target)
            elif target["reachable"]:
                targets[targets.index(existing)] = target
    if failures and not any(target["reachable"] for target in targets):
        raise failures[0]
    return targets


def _project_targets(asset, transport, *, key, deadline, probe_timeout, proc_root):
    root = Path(asset.metadata["path"]).resolve(strict=True)
    targets = []
    for listener in listeners(proc_root, timeout=remaining(deadline, 5)):
        if not listener["cwd"] or listener["owner_type"] == "unknown":
            continue
        cwd = Path(listener["cwd"]).resolve()
        if not cwd.is_relative_to(root):
            continue
        role = (
            listener.get("unit")
            or f"{cwd.relative_to(root)}:{listener['process_name']}"
        )
        target = _listener_target(
            listener,
            transport,
            key=key,
            role=role,
            deadline=deadline,
            probe_timeout=probe_timeout,
            source="project_listener",
            instance=[root.stat().st_dev, root.stat().st_ino],
        )
        _merge_target(targets, target)
    return targets


def _merge_target(targets, target):
    existing = next((item for item in targets if item["id"] == target["id"]), None)
    if existing is None:
        targets.append(target)
    elif not existing["reachable"] and target["reachable"]:
        targets[targets.index(existing)] = target


def _listener_target(
    listener, transport, *, key, role, deadline, probe_timeout, source, instance
):
    port = listener["port"]
    host = ipaddress.ip_address(listener["address"].replace("*", "0.0.0.0"))
    if host.is_unspecified:
        host = ipaddress.ip_address("::1" if host.version == 6 else "127.0.0.1")
    authority = f"[{host}]" if host.version == 6 else str(host)
    origin = f"http://{authority}:{port}"
    try:
        checked = probe(
            transport, origin + "/", timeout=remaining(deadline, probe_timeout)
        )
    except (httpx.HTTPError, CFError) as exc:
        checked = {
            "reachable": False,
            "status_code": None,
            "fingerprint": None,
            "reason": str(exc)
            if isinstance(exc, CFError)
            else f"项目入口读取失败 ({type(exc).__name__})",
        }
    return {
        "id": digest([key, role, port, "http"]),
        "revision": digest(
            [key, listener["pid"], listener["start_time"], instance, origin]
        ),
        "name": f"{role} · {port}/tcp",
        "origin": origin,
        "path": "/",
        "source": source,
        **checked,
    }
