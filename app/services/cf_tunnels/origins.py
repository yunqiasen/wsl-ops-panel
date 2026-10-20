"""Read actual runtime bindings; display links and EXPOSE are not host bindings."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
import subprocess
from typing import Any

import httpx

from app.models.assets import AssetSnapshot

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
        raise ValueError("项目尚缺少可确认的 Docker 归属")
    return digest([asset.category, str(Path(directory).resolve())])


def inspect_container(name: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["docker", "container", "inspect", "--format", _DOCKER_FIELDS, name],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    result = json.loads(completed.stdout)
    if not isinstance(result, dict):
        raise ValueError("Docker 运行信息格式错误")
    return result


def probe(client: httpx.Client, url: str) -> dict[str, object]:
    with client.stream("GET", url, timeout=5, follow_redirects=False) as response:
        body = bytearray()
        for part in response.iter_bytes(chunk_size=8192):
            body.extend(part[: 65536 - len(body)])
            if len(body) >= 65536:
                break
        return {
            "status_code": response.status_code,
            "reachable": 200 <= response.status_code < 300,
            "fingerprint": digest(
                [
                    response.headers.get("content-type", "").split(";")[0],
                    bytes(body).hex(),
                ]
            ),
        }


def prepare_targets(asset: AssetSnapshot, client: httpx.Client) -> dict[str, object]:
    key = owner_key(asset)
    primary = next(
        (c for c in asset.containers if c.name == asset.primary_container_name), None
    )
    if primary is None:
        raise ValueError("项目尚缺少主容器")
    info = inspect_container(primary.name)
    labels = info.get("labels") or {}
    expected_dir = str(Path(str(asset.metadata["project_dir"])).resolve())
    observed_dir = labels.get("com.docker.compose.project.working_dir")
    if (
        not observed_dir
        or str(Path(observed_dir).resolve()) != expected_dir
        or labels.get("com.docker.compose.project") != primary.compose_project
        or labels.get("com.docker.compose.service") != primary.compose_service
    ):
        raise ValueError("容器归属已变化，请刷新项目后重试")
    if not info.get("running"):
        raise ValueError("项目容器尚未运行")
    targets = []
    for exposed, bindings in (info.get("ports") or {}).items():
        if not exposed.endswith("/tcp") or not isinstance(bindings, list):
            continue
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
            checked = probe(client, f"{origin}/")
            target = {
                "id": digest([key, primary.compose_service, exposed, "http"]),
                "revision": digest([key, info["id"], info.get("started_at"), origin]),
                "name": f"{primary.compose_service} · {exposed}",
                "origin": origin,
                "path": "/",
                "source": "published_port",
                **checked,
            }
            if not any(t["id"] == target["id"] for t in targets):
                targets.append(target)
    return {"targets": targets, "selected_id": None}
