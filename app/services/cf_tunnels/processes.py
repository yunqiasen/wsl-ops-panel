"""Bind project entries to current listening processes, never to saved port hints."""

from pathlib import Path
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
                cwd = (proc_root / pid / "cwd").resolve(strict=True)
                cgroup = (proc_root / pid / "cgroup").read_text()
            except OSError:
                continue
            # A reused PID is not evidence for the socket observed above.
            if identity != process_identity(int(pid), proc_root=proc_root):
                continue
            result.append(
                {
                    **identity,
                    "cwd": str(cwd),
                    "cgroup": cgroup,
                    "address": socket["local_address"],
                    "port": int(socket["port"]),
                }
            )
    return result
