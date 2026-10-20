"""On-demand CF access, independent from the local application's lifecycle."""

from datetime import UTC, datetime
from pathlib import Path
from subprocess import SubprocessError
from time import monotonic, sleep
from uuid import uuid4

import httpx

from app.models.assets import AssetSnapshot
from app.services.agent_native_lock import exclusive_file_lock
from app.services.cf_tunnels.models import CFError, CFOperation, TargetChoice
from app.services.cf_tunnels.origins import owner_key, prepare_targets, remaining
from app.services.cf_tunnels.http_probe import probe
from app.services.cf_tunnels.storage import CFStore
from app.services.cf_tunnels.locks import observe_lock

CF_ACTIONS = {"cf_create", "cf_refresh", "cf_disable"}


class CFTunnelService:
    def __init__(self, data_root: Path | str) -> None:
        from app.services.cf_tunnels.runtime import SystemdTunnelRuntime

        self.data_root = Path(data_root)
        self.store = CFStore(self.data_root / "state.db")
        self.http_transport: httpx.AsyncBaseTransport | None = None
        self.proc_root = Path("/proc")
        self.prepare_timeout = 15.0
        self.probe_timeout = 5.0
        self.runtime = SystemdTunnelRuntime(self.data_root / "cf")
        self.public_timeout = 90.0

    def status(self, asset: AssetSnapshot) -> dict[str, object]:
        capable = (asset.category == "docker" and bool(asset.containers)) or (
            asset.category == "project" and bool(asset.metadata.get("path"))
        )
        try:
            key = owner_key(asset)
        except ValueError:
            key = None
        record = self.store.get(key) if key else None
        pending = self.store.pending(key) if record else []
        if record and record.get("handle") and not pending:
            with observe_lock(
                self.data_root / "cf" / f"{key}.runtime.lock"
            ) as acquired:
                if acquired:
                    record = self._observe_record(key, self.store.get(key))
        state = (record or {}).get("state", "off")
        if pending:
            state = "stopping" if pending[-1][1] == "cf_disable" else "starting"
        return {
            "object_id": asset.object_id,
            "capable": capable,
            "management": "panel" if record else "none",
            "desired": (record or {}).get("desired", "off"),
            "state": state,
            "current_url": record.get("current_url")
            if record and state == "online"
            else None,
            "history_url": (record or {}).get("history_url"),
            "last_verified_at": (record or {}).get("last_verified_at"),
            "last_task_id": (record or {}).get("last_task_id"),
            "error": (record or {}).get("error"),
            "pending_action": pending[-1][1] if pending else None,
        }

    def _observe_record(self, key, record):
        handle = record.get("handle")
        if not handle:
            return record
        try:
            observed = self.runtime.observe(handle)
            if not observed["active"]:
                return self.store.update(
                    key,
                    state="error",
                    current_url=None,
                    history_url=record.get("current_url") or record.get("history_url"),
                    handle=None,
                    error={
                        "stage": "runtime",
                        "message": "CF 托管进程已退出，请重新开启",
                    },
                )
        except (CFError, OSError, SubprocessError) as exc:
            error = (
                exc
                if isinstance(exc, CFError)
                else CFError("runtime", f"CF 运行核对失败 ({type(exc).__name__})")
            )
            return self.store.update(
                key,
                state="error",
                current_url=None,
                history_url=record.get("current_url") or record.get("history_url"),
                error=error.as_dict(),
            )
        return record

    def decorate(self, asset: AssetSnapshot) -> AssetSnapshot:
        if asset.category not in {"docker", "project"}:
            return asset
        status = self.status(asset)
        actions = list(asset.supports_actions)
        if status["capable"]:
            actions = list(dict.fromkeys([*actions, *sorted(CF_ACTIONS)]))
        metadata = dict(asset.metadata)
        capabilities = dict(metadata.get("capabilities") or {})
        cf = dict(capabilities.get("cf_tunnel") or {})
        cf.update(
            enabled=status["capable"],
            supported_actions=sorted(CF_ACTIONS) if status["capable"] else [],
            current_url=status["current_url"],
        )
        capabilities["cf_tunnel"] = cf
        metadata.update(cf=status, capabilities=capabilities)
        return asset.model_copy(
            update={"metadata": metadata, "supports_actions": actions}
        )

    def prepare(self, asset: AssetSnapshot) -> dict[str, object]:
        try:
            prepared = prepare_targets(
                asset,
                self.http_transport,
                timeout=self.prepare_timeout,
                probe_timeout=self.probe_timeout,
                proc_root=self.proc_root,
            )
            record = self.store.get(owner_key(asset)) or {}
            saved = record.get("selection_id") or record.get("target", {}).get("id")
            valid = [item for item in prepared["targets"] if item["reachable"]]
            previous_identity = record.get("target", {}).get("project_identity")
            selected = next(
                (
                    item["id"]
                    for item in valid
                    if item["id"] == saved
                    and (
                        not previous_identity
                        or item.get("project_identity") == previous_identity
                    )
                ),
                None,
            )
            if not saved and len(valid) == 1:
                selected = valid[0]["id"]
            prepared.update(
                selected_id=selected, requires_selection=bool(valid) and not selected
            )
            return prepared
        except (httpx.HTTPError, OSError, SubprocessError) as exc:
            raise CFError(
                "origin_probe", f"项目入口读取失败 ({type(exc).__name__})"
            ) from exc

    def enqueue(
        self,
        queue,
        asset: AssetSnapshot,
        action: str,
        choice: TargetChoice | None = None,
    ):
        from app.adapters.base import ActionPlan

        key = owner_key(asset)
        if action != "cf_disable":
            prepared = self.prepare(asset)
            targets = [item for item in prepared["targets"] if item["reachable"]]
            if choice:
                targets = [
                    item
                    for item in targets
                    if item["id"] == choice.id and item["revision"] == choice.revision
                ]
            elif prepared.get("selected_id"):
                targets = [
                    item for item in targets if item["id"] == prepared["selected_id"]
                ]
            elif prepared.get("requires_selection"):
                raise CFError("origin_selection", "需要重新选择一个当前可达的项目入口")
            if len(targets) != 1:
                raise CFError(
                    "origin_selection",
                    "需要选择一个当前可达的项目入口"
                    if targets
                    else "没有有效入口，请确认项目运行状态并重新选择",
                )
            choice = TargetChoice(id=targets[0]["id"], revision=targets[0]["revision"])
        operation = CFOperation(
            request_id=str(uuid4()),
            owner=key,
            action=action,
            asset=asset,
            target=choice,
        )
        plan = ActionPlan(
            commands=[], cf_operation=operation, command_timeout_seconds=120
        )
        with exclusive_file_lock(self.data_root / "cf" / f"{key}.enqueue.lock"):
            self.store.accept(operation)
            try:
                task = queue.enqueue(
                    asset.object_id, action, plan=plan, task_id=operation.request_id
                )
            except Exception:
                self.store.settle_request(operation.request_id, "failed")
                self.store.update(
                    key, error={"stage": "persistence", "message": "CF 任务入队失败"}
                )
                raise
        return task, plan

    def execute(self, operation: CFOperation, task) -> None:
        key = operation.owner
        total_deadline = monotonic() + 120
        with exclusive_file_lock(self.data_root / "cf" / f"{key}.runtime.lock"):
            if self.store.request_status(operation.request_id) != "queued":
                raise CFError("persistence", "CF 任务记录已失效")
            self.store.settle_request(operation.request_id, "running")
            record = self.store.get(key) or {}
            if operation.action == "cf_disable":
                self.store.update(
                    key,
                    state="stopping",
                    current_url=None,
                    history_url=record.get("current_url") or record.get("history_url"),
                )
                try:
                    if record.get("handle"):
                        self.runtime.stop(record["handle"], timeout=15)
                    self.store.update(
                        key, state="off", handle=None, error=None, last_verified_at=None
                    )
                    self.store.settle_request(operation.request_id, "succeeded")
                    return
                except Exception as exc:
                    error = (
                        CFError(exc.stage, f"{exc}；原隧道仍待清理")
                        if isinstance(exc, CFError)
                        else CFError(
                            "cleanup",
                            f"CF 关闭失败 ({type(exc).__name__})，原隧道仍待清理",
                        )
                    )
                    self.store.update(key, state="error", error=error.as_dict())
                    self.store.settle_request(operation.request_id, "failed")
                    raise error from exc
            self.store.update(key, state="starting", current_url=None, error=None)
            handle = None
            try:
                targets = self.prepare(operation.asset)["targets"]
                target = next(
                    (
                        item
                        for item in targets
                        if item["reachable"]
                        and operation.target
                        and item["id"] == operation.target.id
                        and item["revision"] == operation.target.revision
                    ),
                    None,
                )
                if target is None:
                    raise CFError("origin_identity", "入口或容器实例已变化，请重新开启")
                existing = record.get("handle")
                if existing and self.runtime.observe(existing)["active"]:
                    if (
                        operation.action == "cf_create"
                        and record.get("target", {}).get("revision")
                        == target["revision"]
                    ):
                        handle = existing
                    else:
                        self.runtime.stop(existing, timeout=15)
                        self.store.update(
                            key,
                            handle=None,
                            history_url=record.get("current_url")
                            or record.get("history_url"),
                        )
                if handle is None:
                    handle = self.runtime.start(
                        key, operation.request_id, target["origin"], timeout=10
                    )
                self.store.update(
                    key,
                    handle=handle,
                    target=target,
                    active_generation=operation.request_id,
                )
                deadline = min(total_deadline, monotonic() + self.public_timeout)
                last_error = CFError("cf_connect", "CF 网址建立超时")
                while monotonic() < deadline:
                    observed = self.runtime.observe(handle)
                    if not observed["active"]:
                        raise CFError("cf_connect", "CF 托管进程已退出")
                    url = observed.get("url")
                    if url:
                        from app.services.cf_tunnels.runtime import quick_url

                        public = quick_url(url) + target["path"]
                        try:
                            checked = probe(
                                self.http_transport,
                                public,
                                timeout=remaining(deadline, self.probe_timeout),
                                stage="public_probe",
                            )
                            if (
                                checked["reachable"]
                                and checked["fingerprint"] == target["fingerprint"]
                            ):
                                self.store.update(
                                    key,
                                    state="online",
                                    current_url=public,
                                    last_verified_at=datetime.now(UTC).isoformat(),
                                    error=None,
                                )
                                self.runtime.activate(handle)
                                self.store.settle_request(
                                    operation.request_id, "succeeded"
                                )
                                return
                            last_error = CFError(
                                "public_probe", "公网响应与项目入口不一致"
                            )
                        except (httpx.HTTPError, CFError) as exc:
                            last_error = CFError(
                                "public_probe",
                                f"公网访问核验失败 ({type(exc).__name__})",
                            )
                    sleep(min(0.2, max(0, deadline - monotonic())))
                raise last_error
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, CFError)
                    else CFError("runtime", f"CF 执行失败 ({type(exc).__name__})")
                )
                if handle:
                    try:
                        self.runtime.stop(handle)
                        self.store.update(key, handle=None)
                    except Exception:
                        error = CFError("cleanup", f"{error}；隧道仍待清理")
                self.store.update(
                    key, state="error", current_url=None, error=error.as_dict()
                )
                self.store.settle_request(operation.request_id, "failed")
                raise error from exc
