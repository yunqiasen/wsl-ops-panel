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

from app.models.assets import AssetSnapshot
from app.services.cf_tunnels.http_probe import probe
from app.services.cf_tunnels.models import CFError

_DOCKER_FIELDS = (
    '{"id":{{json .Id}},"name":{{json .Name}},"running":{{json .State.Running}},'
    '"started_at":{{json .State.StartedAt}},"network_mode":{{json .HostConfig.NetworkMode}},'
    '"ports":{{json .NetworkSettings.Ports}},"networks":{{json .NetworkSettings.Networks}},'
    '"labels":{{json .Config.Labels}}}'
)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def owner_key(asset: AssetSnapshot) -> str:
    directory = asset.metadata.get("project_dir") or asset.metadata.get("path")
    if asset.category != "docker" or not isinstance(directory, str) or not directory:
        raise CFError("origin_identity", "项目尚缺少可确认的 Docker 归属")
    return digest([asset.category, str(Path(directory).resolve())])


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
) -> dict[str, object]:
    deadline = monotonic() + timeout
    key = owner_key(asset)
    primary = next(
        (c for c in asset.containers if c.name == asset.primary_container_name), None
    )
    if primary is None:
        raise CFError("origin_identity", "项目尚缺少主容器")
    info = inspect_container(primary.name, timeout=remaining(deadline, 5))
    labels = info.get("labels") or {}
    expected_dir = str(Path(str(asset.metadata["project_dir"])).resolve())
    observed_dir = labels.get("com.docker.compose.project.working_dir")
    if (
        not observed_dir
        or str(Path(observed_dir).resolve()) != expected_dir
        or labels.get("com.docker.compose.project") != primary.compose_project
        or labels.get("com.docker.compose.service") != primary.compose_service
    ):
        raise CFError("origin_identity", "容器归属已变化，请刷新项目后重试")
    if not info.get("running"):
        raise CFError("origin_probe", "项目容器尚未运行")
    targets = []
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
            checked = probe(
                transport, f"{origin}/", timeout=remaining(deadline, probe_timeout)
            )
            target = {
                "id": digest([key, primary.compose_service, exposed, "http"]),
                "revision": digest([key, info["id"], info.get("started_at"), origin]),
                "name": f"{primary.compose_service} · {exposed}",
                "origin": origin,
                "path": "/",
                "source": source,
                **checked,
            }
            if not any(t["id"] == target["id"] for t in targets):
                targets.append(target)
    return {"targets": targets, "selected_id": None}
