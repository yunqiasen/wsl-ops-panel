from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx

from app.services.agent_route_takeover import AgentRouteTakeover
from app.services.agent_router_config import AgentRouterConfigStore

ROUTER_SERVICE_NAME = "wsl-agent-router.service"


class AgentRouterControlError(RuntimeError):
    pass


class ActiveTakeoverError(AgentRouterControlError):
    def __init__(self, clients: list[str]) -> None:
        self.clients = clients
        super().__init__(f"active client takeovers: {', '.join(clients)}")


class AgentRouterController:
    def __init__(
        self,
        store: AgentRouterConfigStore,
        *,
        home: Path | str | None = None,
        state_root: Path | str | None = None,
        runner: Callable[[list[str]], Any] | None = None,
        health_probe: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.home = Path(home) if home is not None else Path.home()
        self.state_root = (
            Path(state_root) if state_root is not None else store.data_root / "takeover"
        )
        self._runner = runner or _run_command
        self._health_probe = health_probe or self._probe_health
        self.takeover = AgentRouteTakeover(self.home, self.state_root)

    def configure(self, **values: Any) -> dict[str, Any]:
        return self.store.update_global(**values)

    def start(self) -> dict[str, Any]:
        return self._lifecycle("start")

    def restart(self) -> dict[str, Any]:
        return self._lifecycle("restart")

    def stop(self, *, restore_clients: bool = False) -> dict[str, Any]:
        active = sorted(self.takeover.status())
        if active and not restore_clients:
            raise ActiveTakeoverError(active)
        restored: list[str] = []
        if restore_clients:
            for client_id in active:
                self.takeover.disable(client_id)
                self.store.set_takeover(client_id, False)
                restored.append(client_id)
        result = self._lifecycle("stop")
        result["restored_clients"] = restored
        return result

    def status(self) -> dict[str, Any]:
        config = self.store.public_snapshot()
        health = self._health_probe()
        healthy = str(health.get("status") or "") == "ok"
        conflict = False if healthy else self._port_in_use(
            str(config["listen_address"]), int(config["listen_port"])
        )
        service = "running" if healthy else "port_conflict" if conflict else "stopped"
        return {
            "service": service,
            "healthy": healthy,
            "port_conflict": conflict,
            "health": health,
            "config": config,
            "takeover": self.takeover.status(),
        }

    def enable_takeover(self, client_id: str) -> dict[str, Any]:
        health = self._health_probe()
        if str(health.get("status") or "") != "ok":
            raise AgentRouterControlError("router service is not healthy")
        config = self.store.snapshot()
        route_path = _route_path(client_id)
        route_url = (
            f"http://{config['listen_address']}:{config['listen_port']}"
            f"{route_path}"
        )
        result = self.takeover.enable(client_id, route_url)
        self.store.set_takeover(client_id, True)
        return {"takeover": self.takeover.status(), "result": result}

    def disable_takeover(self, client_id: str) -> dict[str, Any]:
        result = self.takeover.disable(client_id)
        self.store.set_takeover(client_id, False)
        return {"takeover": self.takeover.status(), "result": result}

    def _lifecycle(self, action: str) -> dict[str, Any]:
        command = ["sudo", "-n", "systemctl", action, ROUTER_SERVICE_NAME]
        try:
            outcome = self._runner(command)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "action": action, "error": "command_failed"}
        code = _return_code(outcome)
        return {
            "ok": code == 0,
            "action": action,
            "service": ROUTER_SERVICE_NAME,
        }

    def _probe_health(self) -> dict[str, Any]:
        config = self.store.snapshot()
        url = f"http://{config['listen_address']}:{config['listen_port']}/health"
        try:
            response = httpx.get(url, timeout=1.5)
            if 200 <= response.status_code < 300:
                return {"status": "ok"}
        except httpx.HTTPError:
            pass
        return {"status": "unreachable"}

    @staticmethod
    def _port_in_use(address: str, port: int) -> bool:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(0.1)
            return sock.connect_ex((address, port)) == 0
        except OSError:
            return False
        finally:
            sock.close()


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _return_code(value: Any) -> int:
    if isinstance(value, bool):
        return 0 if value else 1
    if isinstance(value, int):
        return value
    return int(getattr(value, "returncode", 1))


def _route_path(client_id: str) -> str:
    from app.services.agent_clients import get_agent_client

    client = get_agent_client(client_id)
    if client is None or not client.route_path or "route" not in client.write_support:
        raise AgentRouterControlError(f"client route is not supported: {client_id}")
    return client.route_path
