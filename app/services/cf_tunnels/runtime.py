"""User-level transient services, with exact invocation ownership checks."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from time import monotonic, sleep

from app.services.cf_tunnels.models import CFError


def quick_url(value: str) -> str:
    if not re.fullmatch(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com", value):
        raise CFError("cf_connect", "CF 返回的网址格式不正确")
    return value


def budget(deadline: float, cap: float = 5, *, stage: str = "runtime") -> float:
    left = deadline - monotonic()
    if left <= 0:
        raise CFError(stage, "CF 运行操作超时")
    return min(cap, left)


class SystemdTunnelRuntime:
    def __init__(self, root: Path):
        self.root = root

    @property
    def boot_id(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def _show(self, unit: str, timeout: float = 5) -> dict[str, str]:
        try:
            result = subprocess.run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    unit,
                    "--property=LoadState,ActiveState,MainPID,InvocationID,Description,Transient",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CFError("runtime", "CF 运行标识读取超时") from exc
        if result.returncode:
            raise CFError("runtime", "用户级服务管理器访问失败")
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    def _directory(self, handle: dict) -> Path:
        return self.root / handle["owner"] / handle["token"]

    def start(self, owner: str, token: str, origin: str, *, timeout: float) -> dict:
        deadline = monotonic() + timeout
        binary = shutil.which("cloudflared") or str(
            Path.home() / ".cftunnel/bin/cloudflared"
        )
        if not os.access(binary, os.X_OK):
            raise CFError(
                "dependency", "缺少可执行的 cloudflared，请在系统基础设施中安装"
            )
        unit = f"wsl-ops-cf-{owner[:24]}.service"
        before = self._show(unit, budget(deadline))
        if before.get("ActiveState") in {"active", "activating", "deactivating"}:
            raise CFError("runtime", "对应 CF 运行资源已被占用")
        handle = {"owner": owner, "token": token, "unit": unit, "boot_id": self.boot_id}
        directory = self._directory(handle)
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
        description = f"wsl-ops-cf:{owner}:{token}"
        command = [
            "systemd-run",
            "--user",
            "--quiet",
            f"--unit={unit}",
            f"--description={description}",
            "--collect",
            "--property=Type=exec",
            "--property=Restart=no",
            "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s",
            "--property=UMask=0077",
            "--property=StandardOutput=null",
            "--property=StandardError=null",
            str(Path(sys.executable).resolve()),
            str(Path(__file__).with_name("runner.py")),
            "--directory",
            str(directory),
            "--token",
            token,
            "--binary",
            binary,
            "--origin",
            origin,
        ]
        try:
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=budget(deadline, timeout),
                )
            except subprocess.TimeoutExpired as exc:
                raise CFError("runtime", "CF 用户级服务创建超时") from exc
            if result.returncode:
                raise CFError("runtime", "CF 用户级服务创建失败")
            while monotonic() < deadline:
                info = self._show(unit, budget(deadline))
                if info.get("Description") == description and info.get("InvocationID"):
                    handle["invocation_id"] = info["InvocationID"]
                    return handle
                sleep(min(0.05, max(0, deadline - monotonic())))
            raise CFError("runtime", "CF 运行标识登记超时")
        except (CFError, OSError, subprocess.SubprocessError) as exc:
            error = (
                exc
                if isinstance(exc, CFError)
                else CFError("runtime", f"CF 用户级服务创建失败 ({type(exc).__name__})")
            )
            # Even if systemd-run/registration times out, the unit may exist.
            # Retain this unique generation so the caller can verify and clean it.
            error.handle = handle
            raise error from exc

    def _owned_info(self, handle: dict, *, timeout: float) -> dict:
        if handle["boot_id"] != self.boot_id:
            return {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}
        info = self._show(handle["unit"], timeout)
        active = info.get("ActiveState") in {"active", "activating", "deactivating"}
        if active and (
            not info.get("InvocationID")
            or (
                handle.get("invocation_id")
                and info.get("InvocationID") != handle["invocation_id"]
            )
            or info.get("Description")
            != f"wsl-ops-cf:{handle['owner']}:{handle['token']}"
            or info.get("Transient") != "yes"
        ):
            raise CFError("runtime_identity", "CF 服务归属已变化，原资源保持原样")
        if active:
            handle.setdefault("invocation_id", info["InvocationID"])
        return info

    def recover(self, owner: str, token: str, *, timeout: float = 5) -> dict | None:
        handle = {
            "owner": owner,
            "token": token,
            "unit": f"wsl-ops-cf-{owner[:24]}.service",
            "boot_id": self.boot_id,
        }
        if not self._directory(handle).is_dir():
            return None
        info = self._owned_info(handle, timeout=timeout)
        if info.get("ActiveState") not in {"active", "activating", "deactivating"}:
            return None
        return handle

    def observe(self, handle: dict, *, timeout: float = 5) -> dict:
        info = self._owned_info(handle, timeout=timeout)
        active = info.get("ActiveState") in {"active", "activating", "deactivating"}
        url = None
        if active:
            try:
                value = json.loads((self._directory(handle) / "url.json").read_text())
                if value.get("token") == handle["token"]:
                    url = quick_url(value["url"])
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        return {"active": active and int(info.get("MainPID") or 0) > 0, "url": url}

    def activate(self, handle: dict, *, timeout: float = 5) -> None:
        if not self.observe(handle, timeout=timeout)["active"]:
            raise CFError("runtime", "核验完成时 CF 进程已退出")
        path = self._directory(handle) / "verified"
        path.write_text(handle["token"])
        path.chmod(0o600)

    def stop(self, handle: dict, *, timeout: float = 15) -> None:
        deadline = monotonic() + timeout
        live_states = {"active", "activating", "deactivating"}
        try:
            info = self._owned_info(handle, timeout=budget(deadline, stage="cleanup"))
            if info.get("ActiveState") not in live_states:
                return
            result = subprocess.run(
                ["systemctl", "--user", "stop", handle["unit"]],
                capture_output=True,
                text=True,
                timeout=budget(deadline, timeout, stage="cleanup"),
            )
            if result.returncode:
                raise CFError("cleanup", "CF 服务关闭失败，仍待清理")
            info = self._owned_info(handle, timeout=budget(deadline, stage="cleanup"))
            if info.get("ActiveState") in live_states:
                raise CFError("cleanup", "CF 服务关闭后仍在运行，仍待清理")
        except subprocess.TimeoutExpired as exc:
            raise CFError("cleanup", "CF 服务关闭超时，仍待清理") from exc
        except CFError as exc:
            if exc.stage == "runtime":
                raise CFError("cleanup", f"CF 关闭核对失败：{exc}") from exc
            raise
