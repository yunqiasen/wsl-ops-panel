"""Bind project entries to current listening processes, never to saved port hints."""

from pathlib import Path
import os
import re
import subprocess

from app.scanners.host_process_scanner import parse_listening_socket
from app.services.host_ownership import process_identity


def listeners(proc_root: Path, *, timeout: float) -> list[dict]:
    observed = subprocess.run(
        ["ss", "-H", "-ltnp"],
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    result = []
    for line in observed.stdout.splitlines():
        try:
            socket = parse_listening_socket(line).metadata
        except ValueError:
            continue
        for pid in dict.fromkeys(re.findall(r"\bpid=(\d+)", line)):
            identity = process_identity(int(pid), proc_root=proc_root)
            if not identity:
                continue
            try:
                cwd = os.readlink(proc_root / pid / "cwd")
            except OSError:
                cwd = None
            try:
                cgroup = (proc_root / pid / "cgroup").read_text()
            except OSError:
                continue
            # A reused PID is not evidence for the socket observed above.
            if identity != process_identity(int(pid), proc_root=proc_root):
                continue
            result.append(
                {
                    **identity,
                    "cwd": cwd,
                    "cgroup": cgroup,
                    "address": socket["local_address"],
                    "port": int(socket["port"]),
                }
            )
    return result


def project_role(
    listener: dict, root: Path, metadata: dict, *, timeout: float
) -> str | None:
    if listener["owner_type"] == "unknown":
        return None
    if listener["cwd"]:
        cwd = Path(listener["cwd"]).resolve()
        if cwd.is_relative_to(root):
            return (
                listener.get("unit")
                or f"{cwd.relative_to(root)}:{listener['process_name']}"
            )
    if listener["owner_type"] != "systemd":
        return None
    targets = metadata.get("service_targets") or [
        {
            "name": metadata.get("service_unit"),
            "scope": metadata.get("service_scope", "system"),
        }
    ]
    if not any(
        item.get("name") == listener["unit"]
        and item.get("scope", "system") == listener["service_scope"]
        for item in targets
    ):
        return None
    control = (
        ["systemctl", "--user"]
        if listener["service_scope"] == "user"
        else ["systemctl"]
    )
    observed = subprocess.run(
        [
            *control,
            "show",
            listener["unit"],
            "--property=ActiveState,WorkingDirectory,ControlGroup,InvocationID",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    info = dict(
        line.split("=", 1) for line in observed.stdout.splitlines() if "=" in line
    )
    directory = info.get("WorkingDirectory")
    group = info.get("ControlGroup")
    groups = [row.split(":", 2)[-1] for row in listener["cgroup"].splitlines()]
    if (
        info.get("ActiveState") == "active"
        and info.get("InvocationID")
        and directory
        and Path(directory).is_absolute()
        and Path(directory).resolve().is_relative_to(root)
        and group
        and group != "/"
        and any(value == group or value.startswith(group + "/") for value in groups)
    ):
        return listener["unit"]
    return None
