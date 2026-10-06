"""Listening ports are observations, never authority to select a named service."""

import ctypes
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import time


COMPOSE = "com.docker.compose."


def process_identity(pid, *, proc_root=Path("/proc")):
    root = Path(proc_root) / str(pid)
    try:
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        result = dict(
            pid=int(pid),
            process_name=(root / "comm").read_text().strip(),
            start_time=fields[19],
        )
        cgroup = (root / "cgroup").read_text()
        units = [
            part
            for line in cgroup.splitlines()
            for part in line.split("/")
            if part.endswith(".service")
        ]
        units = [unit for unit in units if not unit.startswith("user@")]
        if re.search(r"(?:docker[-/]|libpod-|kubepods|cri-containerd-)", cgroup):
            result["owner_type"] = "unknown"
        elif units:
            user_scope = "/user.slice/" in cgroup
            user = re.search(r"/user-(\d+)\.slice/", cgroup)
            if user_scope and (not user or int(user.group(1)) != os.getuid()):
                result["owner_type"] = "unknown"
                return result
            result.update(
                owner_type="systemd",
                unit=units[-1],
                service_scope="user" if user_scope else "system",
            )
        else:
            result["owner_type"] = "process"
        return result
    except (OSError, ValueError, IndexError):
        return {}


def docker_context(asset, containers, *, proc_root=Path("/proc")):
    meta = asset.metadata
    try:
        words = (
            (Path(proc_root) / str(meta["pid"]) / "cmdline")
            .read_bytes()
            .decode()
            .split("\0")
        )
        ip = words[words.index("-container-ip") + 1]
        target_port = words[words.index("-container-port") + 1]
    except (OSError, ValueError, IndexError):
        return None
    matches = []
    for container in containers:
        network = container.get("NetworkSettings") or {}
        addresses = {
            a.get("IPAddress") for a in (network.get("Networks") or {}).values()
        }
        if ip not in addresses:
            continue
        bindings = (network.get("Ports") or {}).get(f"{target_port}/tcp") or []
        if any(
            str(b.get("HostPort")) == str(meta["port"])
            and (b.get("HostIp") or "0.0.0.0") == meta["local_address"]
            for b in bindings
        ):
            matches.append(container)
    if len(matches) != 1:
        return None
    primary = matches[0]
    labels = primary.get("Config", {}).get("Labels") or {}
    project = labels.get(COMPOSE + "project")
    cwd = labels.get(COMPOSE + "project.working_dir") or "/"
    selected = (
        [primary]
        if not project
        else [
            c
            for c in containers
            if (c.get("Config", {}).get("Labels") or {}).get(COMPOSE + "project")
            == project
            and (c.get("Config", {}).get("Labels") or {}).get(
                COMPOSE + "project.working_dir"
            )
            == cwd
            and (c.get("Config", {}).get("Labels") or {})
            .get(COMPOSE + "oneoff", "")
            .lower()
            != "true"
        ]
    )
    return dict(
        primary_container=primary["Name"].lstrip("/"),
        project=project,
        compose_service=labels.get(COMPOSE + "service"),
        project_dir=cwd,
        compose_files=labels.get(COMPOSE + "project.config_files", "").split(",")
        if labels.get(COMPOSE + "project.config_files")
        else [],
        containers=[
            dict(id=c["Id"], name=c["Name"].lstrip("/"))
            for c in sorted(selected, key=lambda item: item["Id"])
        ],
    )


def inspect_containers(runner=subprocess.run):
    ids = runner(
        ["docker", "ps", "-aq", "--no-trunc"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if ids.returncode:
        raise RuntimeError("容器归属读取失败")
    if not ids.stdout.strip():
        return []
    result = runner(
        ["docker", "container", "inspect", *ids.stdout.split()],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("容器归属读取失败")
    return json.loads(result.stdout)


def attach_owner(asset, *, containers=(), proc_root=Path("/proc")):
    meta = asset.metadata
    identity = process_identity(meta.get("pid"), proc_root=proc_root)
    if not identity or identity["process_name"] != meta.get("process_name"):
        return asset.model_copy(update={"metadata": {**meta, "owner_type": "unknown"}})
    owner = {
        **identity,
        "port": str(meta["port"]),
        "local_address": meta["local_address"],
    }
    if identity["process_name"] == "docker-proxy":
        context = docker_context(asset, containers, proc_root=proc_root)
        if not context:
            return asset.model_copy(
                update={"metadata": {**meta, "owner_type": "unknown"}}
            )
        owner.update(owner_type="docker", docker_context=context)
    # Target fields are only derived from process/cgroup/inspect evidence.
    return asset.model_copy(
        update={
            "metadata": {
                **meta,
                "owner_type": owner["owner_type"],
                "owner_snapshot": owner,
                "target_unit_name": owner.get("unit"),
                "target_container_name": (owner.get("docker_context") or {}).get(
                    "primary_container"
                ),
            }
        }
    )


def _same_process(snapshot, proc_root):
    actual = process_identity(snapshot.get("pid"), proc_root=proc_root)
    if not snapshot.get("start_time") or any(
        actual.get(k) != snapshot.get(k) for k in ("pid", "process_name", "start_time")
    ):
        raise ValueError("监听进程已变化或缺少身份信息，请刷新后重试")
    return actual


def execute(snapshot, action, *, runner=subprocess.run, proc_root=Path("/proc")):
    if action not in {"start", "stop"}:
        raise ValueError("unsupported host action")
    actual = _same_process(snapshot, proc_root)
    from app.scanners.host_process_scanner import parse_listening_socket

    result = runner(["ss", "-ltnp"], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("监听状态读取失败")
    listener = None
    for line in result.stdout.splitlines():
        try:
            asset = parse_listening_socket(line)
        except ValueError:
            continue
        if all(
            str(asset.metadata.get(k)) == str(snapshot.get(k))
            for k in ("pid", "process_name", "port", "local_address")
        ):
            listener = asset
            break
    if listener is None:
        raise ValueError("端口监听已变化，请刷新后重试")
    kind = snapshot.get("owner_type")
    if kind == "docker":
        context = snapshot.get("docker_context")
        current = docker_context(
            listener, inspect_containers(runner), proc_root=proc_root
        )
        if not context or not current or context != current:
            raise ValueError("容器归属已变化，请刷新后重试")
        from app.services.docker_lifecycle import execute as execute_docker

        execute_docker(context, action, runner=runner)
    elif kind == "systemd":
        if any(
            actual.get(k) != snapshot.get(k)
            for k in ("owner_type", "unit", "service_scope")
        ):
            raise ValueError("运行项归属已变化，请刷新后重试")
        from app.adapters.project_adapter import ProjectAdapter

        plan = ProjectAdapter(
            project_dir="/",
            service_unit=actual["unit"],
            service_scope=actual["service_scope"],
        ).plan_action(action)
        for command in plan.commands:
            if command[0] == "sudo":
                command = ["sudo", "-n", *command[1:]]
            result = runner(command, capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError("运行项操作失败")
        control = (
            ["systemctl", "--user"]
            if actual["service_scope"] == "user"
            else ["systemctl"]
        )
        result = runner(
            [*control, "is-active", actual["unit"]],
            capture_output=True,
            text=True,
            timeout=10,
        )
        wanted = {"active"} if action == "start" else {"inactive", "failed"}
        if result.stdout.strip() not in wanted or result.returncode not in (
            {0} if action == "start" else {3}
        ):
            raise RuntimeError("运行项状态核验失败")
    elif (
        kind == "process"
        and actual.get("owner_type") == "process"
        and actual["process_name"] != "docker-proxy"
        and action == "stop"
    ):
        # Pin the kernel process object, not a reusable integer PID.
        fd = _open_process_handle(actual["pid"])
        try:
            _same_process(snapshot, proc_root)
            _signal_process_handle(fd)
        finally:
            os.close(fd)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = process_identity(actual["pid"], proc_root=proc_root)
            if current.get("start_time") != actual["start_time"]:
                break
            # An exited child may remain a zombie until its parent reaps it.
            stat = Path(proc_root) / str(actual["pid"]) / "stat"
            try:
                if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                    break
            except FileNotFoundError:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("进程仍在运行，关闭尚未完成")
    else:
        raise ValueError("监听对象缺少可执行的归属信息")
    print("宿主机操作与状态核验完成")


def _open_process_handle(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    # Some standalone Python builds omit the wrapper despite kernel/libc support.
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        call = libc.pidfd_open
    except AttributeError as exc:
        raise ValueError("当前运行环境缺少可靠进程句柄，保留进程") from exc
    call.argtypes = [ctypes.c_int, ctypes.c_uint]
    call.restype = ctypes.c_int
    fd = call(pid, 0)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return fd


def _signal_process_handle(fd):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        call = libc.pidfd_send_signal
    except AttributeError as exc:
        raise ValueError("当前运行环境缺少可靠进程信号接口，保留进程") from exc
    call.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(fd, signal.SIGTERM, None, 0) < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
