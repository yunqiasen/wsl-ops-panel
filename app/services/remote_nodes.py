from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

import yaml

from app.models.remote_nodes import RemoteNode, RemoteNodeCreate

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class RemoteNodeStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = RLock()
        if self.path.exists():
            self.path.chmod(0o600)

    def list_nodes(self) -> list[RemoteNode]:
        with self._lock:
            payload = self._read_payload()
            nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
            if not isinstance(nodes, list):
                return []
            result: list[RemoteNode] = []
            for item in nodes:
                if isinstance(item, dict):
                    try:
                        result.append(RemoteNode.model_validate(item))
                    except Exception:
                        continue
            return result

    def add_node(self, payload: RemoteNodeCreate) -> RemoteNode:
        with self._lock:
            nodes = self.list_nodes()
            node_id = _unique_id(
                _slugify(payload.name or payload.host), {node.id for node in nodes}
            )
            node = RemoteNode(id=node_id, **payload.model_dump())
            nodes.append(node)
            self._write_nodes(nodes)
            return node

    def delete_node(self, node_id: str) -> bool:
        with self._lock:
            nodes = self.list_nodes()
            kept = [node for node in nodes if node.id != node_id]
            if len(kept) == len(nodes):
                return False
            self._write_nodes(kept)
            return True

    def update_node_status(
        self, node_id: str, *, status: str, error: str | None = None
    ) -> RemoteNode | None:
        with self._lock:
            return self._update_node_status(node_id, status=status, error=error)

    def _update_node_status(
        self, node_id: str, *, status: str, error: str | None = None
    ) -> RemoteNode | None:
        nodes = self.list_nodes()
        updated: RemoteNode | None = None
        checked_at = datetime.now(timezone.utc).isoformat()
        next_nodes: list[RemoteNode] = []
        for node in nodes:
            if node.id == node_id:
                node = node.model_copy(
                    update={
                        "status": status,
                        "last_error": error,
                        "last_checked_at": checked_at,
                    }
                )
                updated = node
            next_nodes.append(node)
        if updated is not None:
            self._write_nodes(next_nodes)
        return updated

    def get_node(self, node_id: str) -> RemoteNode | None:
        return next((node for node in self.list_nodes() if node.id == node_id), None)

    def _read_payload(self) -> dict:
        if not self.path.exists():
            return {"nodes": []}
        payload = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        return payload if isinstance(payload, dict) else {"nodes": []}

    def _write_nodes(self, nodes: list[RemoteNode]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"nodes": [node.model_dump() for node in nodes]}
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


class RemoteSSHService:
    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        credential_root: Path | str = Path("data/sshpass"),
    ) -> None:
        self._runner = runner or _run_command
        self._credential_root = Path(credential_root)

    def test_connection(
        self, node: RemoteNode, *, timeout: float = 8.0
    ) -> tuple[bool, str]:
        command = self._build_ssh_command(node, "echo connected")
        if node.auth_type == "password" and shutil.which("sshpass") is None:
            return False, "password 登录需要安装 sshpass；Key 登录不需要。"
        try:
            completed = self._runner(command, timeout)
        except subprocess.TimeoutExpired:
            return False, f"SSH 连接超时：{node.host}:{node.port}"
        except Exception as exc:
            return False, str(exc)
        output = ((completed.stdout or "") + (completed.stderr or "")).strip()
        if completed.returncode == 0:
            return True, output or "connected"
        return False, output or f"ssh exited with {completed.returncode}"

    def build_command(self, node: RemoteNode, remote_command: str) -> list[str]:
        return self._build_ssh_command(node, remote_command)

    def run_command(
        self, node: RemoteNode, remote_command: str, *, timeout: float = 20.0
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(self._build_ssh_command(node, remote_command), timeout)

    def _build_ssh_command(self, node: RemoteNode, remote_command: str) -> list[str]:
        base = [
            "ssh",
            "-o",
            "ConnectTimeout=6",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-p",
            str(node.port),
        ]
        if node.auth_type == "key" and node.key_path:
            base.extend(["-i", node.key_path, "-o", "BatchMode=yes"])
        if node.auth_type == "password":
            password_file = self._write_password_file(node)
            base = ["sshpass", "-f", str(password_file), *base]
        base.append(f"{node.username}@{node.host}")
        base.append(remote_command)
        return base

    def _write_password_file(self, node: RemoteNode) -> Path:
        self._credential_root.mkdir(parents=True, exist_ok=True)
        self._credential_root.chmod(0o700)
        target = self._credential_root / f"{_slugify(node.id)}.secret"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(node.password or "", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)
        target.chmod(0o600)
        return target


def default_remote_nodes_path(config_root: Path | str) -> Path:
    root = Path(config_root)
    base = root.parent if root.name == "config" else root
    return base / "data" / "remote_nodes.yaml"


def _run_command(
    command: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "node"


def _unique_id(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"
