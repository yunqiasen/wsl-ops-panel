import json
from pathlib import Path
import sys
from typing import Literal

from app.adapters.base import ActionPlan

HostPortAction = Literal["start", "stop"]


class HostPortAdapter:
    def __init__(
        self,
        *,
        port: str,
        owner_type: str | None = None,
        target_asset_id: str | None = None,
        target_unit_name: str | None = None,
        target_container_name: str | None = None,
        pid: int | None = None,
        process_name: str | None = None,
        owner_snapshot: dict | None = None,
    ) -> None:
        # Legacy hints remain accepted, but never authorize a named target.
        self.snapshot = dict(
            owner_snapshot
            or dict(
                port=port, pid=pid, process_name=process_name, owner_type=owner_type
            )
        )

    def plan_action(
        self, action: HostPortAction, version: str | None = None
    ) -> ActionPlan:
        if action not in {"start", "stop"}:
            raise ValueError(f"unsupported host port action: {action}")
        if action == "start" and self.snapshot.get("owner_type") not in {
            "docker",
            "systemd",
        }:
            raise ValueError("独立监听进程没有启动定义")
        return ActionPlan(
            commands=[
                [
                    sys.executable,
                    "-m",
                    "app.tools.host_port_control",
                    action,
                    "--snapshot",
                    json.dumps(self.snapshot),
                ]
            ],
            working_dir=str(Path(__file__).resolve().parents[2]),
            command_timeout_seconds=180,
            requires_sudo=self.snapshot.get("owner_type") == "systemd"
            and self.snapshot.get("service_scope") == "system",
            preview_objects=[
                f"port:{self.snapshot.get('port')}",
                f"pid:{self.snapshot.get('pid')}",
            ],
        )

    def list_available_versions(self) -> list[str]:
        return []
