"""On-demand CF access, independent from the local application's lifecycle."""

from datetime import UTC, datetime
import logging
from pathlib import Path
from subprocess import SubprocessError
from time import monotonic, sleep, time
from uuid import uuid4
from threading import Event, Thread

import httpx

from app.models.assets import AssetSnapshot
from app.services.agent_native_lock import exclusive_file_lock
from app.services.cf_tunnels.models import CFError, CFOperation, TargetChoice
from app.services.cf_tunnels.origins import owner_key, prepare_targets
from app.services.cf_tunnels.http_probe import http_error_message, probe
from app.services.cf_tunnels.storage import CFStore
from app.services.cf_tunnels.locks import observe_lock
from app.services.cf_tunnels.runtime import budget
from app.services.cf_tunnels.legacy import LegacyTunnelRuntime, inspect_legacy

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
        self.legacy_runtime = LegacyTunnelRuntime()
        self.legacy_unit_roots = None
        self.public_timeout = 90.0
        self.clock = time
        self.asset_source = None
        self.task_store = None
        self._observer_stop = Event()
        self._observer_thread = None

    def _runtime_for(self, handle):
        return self.legacy_runtime if handle.get("kind") == "legacy" else self.runtime

    def _legacy(self, asset):
        record = self.store.get(self._key(asset)) or {}
        if record.get("handle") and record["handle"].get("kind") != "legacy":
            return None
        legacy = inspect_legacy(asset, unit_roots=self.legacy_unit_roots)
        if legacy and legacy.get("management") == "external_hint":
            raise CFError("legacy_identity", legacy["reason"])
        if legacy and legacy.get("load_state") == "masked":
            raise CFError("legacy_masked", "旧隧道已被停用 (masked)，原状态保持不变")
        return legacy

    def _key(self, asset):
        key = owner_key(asset)
        if (
            asset.category == "docker"
            and not asset.containers
            and not self.store.get(key)
        ):
            directory = str(Path(asset.metadata["project_dir"]).resolve())
            matches = [
                owner
                for owner, row in self.store.records()
                if row.get("asset", {}).get("object_id") == asset.object_id
                and row.get("asset", {}).get("category") == asset.category
                and str(Path(row["asset"]["metadata"].get("project_dir", "")).resolve())
                == directory
            ]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise CFError(
                    "origin_identity", "项目旧 CF 记录存在多个归属，请重新核对"
                )
        return key

    def status(self, asset: AssetSnapshot) -> dict[str, object]:
        capable = asset.category in {"docker", "project"} and bool(
            asset.metadata.get("project_dir") or asset.metadata.get("path")
        )
        try:
            key = self._key(asset)
        except ValueError:
            key = None
        record = self.store.get(key) if key else None
        legacy = (record or {}).get("legacy")
        if capable and not record:
            try:
                legacy = inspect_legacy(asset, unit_roots=self.legacy_unit_roots)
            except CFError as exc:
                legacy = {"management": "external_hint", "reason": str(exc)}
        reason = None
        if legacy:
            reason = legacy.get("reason") or (
                "旧隧道已被停用 (masked)，原状态保持不变"
                if legacy.get("load_state") == "masked"
                else "由既有 systemd 隧道管理，保留原自启配置"
            )
            if not record and (
                legacy.get("management") == "external_hint"
                or legacy.get("load_state") == "masked"
            ):
                capable = False
        pending = self.store.pending(key) if record else []
        if record:
            with observe_lock(
                self.data_root / "cf" / f"{key}.runtime.lock"
            ) as acquired:
                if acquired:
                    record = self._synchronize_requests(key, self.store.get(key))
                    pending = self.store.pending(key)
                    if record.get("handle") and not pending:
                        record = self._observe_record(key, record)
        state = (record or {}).get("state", "unknown" if legacy else "off")
        if pending:
            state = "stopping" if pending[-1][1] == "cf_disable" else "starting"
        return {
            "object_id": asset.object_id,
            "capable": capable,
            "management": record.get("management", "panel")
            if record
            else (legacy or {}).get("management", "none"),
            "reason": reason,
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

    def _after_host_restart(self, key, record):
        external = bool(record.get("legacy"))
        return self.store.update(
            key,
            state="unknown" if external else "off",
            desired="off",
            handle=None,
            current_url=None,
            last_verified_at=None,
            launch_pending=False,
            boot_id=self.runtime.boot_id,
            error=CFError(
                "legacy_recovery", "宿主已重启，旧隧道由外部管理，当前运行待核验"
            ).as_dict() if external else None,
            history_url=record.get("current_url") or record.get("history_url"),
        )

    def _observe_record(self, key, record):
        handle = record.get("handle")
        if not handle:
            return record
        try:
            if handle.get("boot_id") != self._runtime_for(handle).boot_id:
                return self._after_host_restart(key, record)
            observed = self._runtime_for(handle).observe(handle)
            if not observed["active"]:
                return self.store.update(
                    key,
                    state="off" if record.get("desired") == "off" else "error",
                    current_url=None,
                    history_url=record.get("current_url") or record.get("history_url"),
                    handle=None,
                    error=None
                    if record.get("desired") == "off"
                    else {
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
        if record.get("state") == "online":
            try:
                age = (
                    self.clock()
                    - datetime.fromisoformat(record["last_verified_at"]).timestamp()
                )
            except (KeyError, TypeError, ValueError):
                age = 61
            # ISO timestamps round to microseconds; a frozen clock can be fractionally earlier.
            if not -0.001 <= age < 60:
                return self.store.update(
                    key,
                    state="error",
                    current_url=None,
                    history_url=record.get("current_url") or record.get("history_url"),
                    error={
                        "stage": "freshness",
                        "message": "公网核验已过期，等待重新核验",
                    },
                )
        return record

    def _synchronize_requests(self, key, record):
        # Acceptance spans two stores. A concurrent reader must not mistake the
        # short gap before task insertion for an abandoned request.
        with observe_lock(self.data_root / "cf" / f"{key}.enqueue.lock") as acquired:
            if not acquired:
                return record
            return self._reconcile_requests(key, record)

    def _reconcile_requests(self, key, record):
        pending = self.store.pending(key)
        boot = record.get("boot_id") or (record.get("handle") or {}).get("boot_id")
        changed_boot = bool(boot and boot != self.runtime.boot_id)
        interrupted = False
        for request_id, _ in pending:
            try:
                task = self.task_store.get(request_id) if self.task_store else None
            except KeyError:
                task = None
            if not changed_boot and (
                self.task_store is None
                or (task and task.status in {"queued", "running"})
            ):
                continue
            self.store.settle_request(request_id, "failed")
            interrupted = True
            if changed_boot and task and task.status in {"queued", "running"}:
                task.status = "interrupted"
                task.finished_at = datetime.fromtimestamp(self.clock(), UTC)
                self.task_store.update(task)
        last_request = record.get("last_task_id")
        if (
            self.task_store is not None
            and last_request
            and self.store.request_status(last_request) == "succeeded"
        ):
            try:
                completed = self.task_store.get(last_request)
            except KeyError:
                completed = None
            if not completed or completed.status not in {
                "queued",
                "running",
                "succeeded",
            }:
                self.store.settle_request(last_request, "failed")
                interrupted = True
        if changed_boot:
            return self._after_host_restart(key, record)
        if record.get("launch_pending") and not self.store.pending(key):
            if record.get("legacy"):
                # Without the launch's InvocationID an external unit is not
                # recoverable from our transient namespace. Leave its process
                # alone and retain the explicit close/retry association.
                return self.store.update(
                    key,
                    state="error",
                    current_url=None,
                    handle=None,
                    launch_pending=False,
                    error=CFError(
                        "legacy_recovery",
                        "旧隧道启动登记中断，实际资源待核对；请重试开启或关闭",
                    ).as_dict(),
                )
            try:
                handle = self.runtime.recover(key, record["active_generation"])
                record = self.store.update(key, handle=handle, launch_pending=False)
                interrupted = True
            except (CFError, OSError, SubprocessError) as exc:
                error = (
                    exc
                    if isinstance(exc, CFError)
                    else CFError(
                        "runtime", f"CF 启动残留核对失败 ({type(exc).__name__})"
                    )
                )
                return self.store.update(
                    key, state="error", current_url=None, error=error.as_dict()
                )
        if interrupted:
            return self._cleanup_record(
                key,
                record,
                CFError("persistence", "上次 CF 任务已中断或失效，运行记录已核对"),
            )
        return record

    def recover(self):
        for key, record in self.store.records():
            with observe_lock(
                self.data_root / "cf" / f"{key}.runtime.lock"
            ) as acquired:
                if acquired:
                    self._synchronize_requests(key, self.store.get(key))
        self.maintain(force=True)

    def start_observer(self):
        if self._observer_thread and self._observer_thread.is_alive():
            return
        self._observer_stop.clear()

        def run():
            while not self._observer_stop.wait(5):
                try:
                    self.maintain()
                except Exception as exc:
                    logging.getLogger(__name__).warning(
                        "CF observation failed (%s)", type(exc).__name__
                    )

        self._observer_thread = Thread(
            target=run, name="wsl-ops-cf-observer", daemon=True
        )
        self._observer_thread.start()

    def stop_observer(self):
        self._observer_stop.set()
        if self._observer_thread:
            self._observer_thread.join(timeout=2)

    def maintain(self, *, force=False):
        if self.asset_source is None:
            return
        for key, record in self.store.records():
            if not record.get("handle") and not record.get("launch_pending"):
                continue
            with observe_lock(
                self.data_root / "cf" / f"{key}.runtime.lock"
            ) as acquired:
                if not acquired:
                    continue
                record = self._synchronize_requests(key, self.store.get(key))
                if self.store.pending(key):
                    continue
                record = self._observe_record(key, record)
                if not record.get("handle"):
                    continue
                if record.get("desired") == "off":
                    try:
                        self._close_record(key, record)
                    except CFError:
                        pass  # The exact resource and cleanup error remain for retry.
                    continue
                try:
                    object_id = record["asset"]["object_id"]
                    assets = self.asset_source()
                    asset = assets.get_assets([object_id]).get(object_id)
                    if asset is None:
                        asset = next(
                            (
                                item
                                for item in assets.list_assets(
                                    record["asset"]["category"]
                                )
                                if owner_key(item) == key
                            ),
                            None,
                        )
                    if asset is None or owner_key(asset) != key:
                        raise CFError("origin_identity", "原项目已消失或归属变化")
                    prepared = self.prepare(asset)
                    target = next(
                        (
                            item
                            for item in prepared["targets"]
                            if item["reachable"]
                            and item["revision"] == record["target"]["revision"]
                        ),
                        None,
                    )
                    if target is None:
                        raise CFError(
                            "origin_identity", "项目入口或运行实例已变化，请重新开启"
                        )
                except (CFError, OSError, SubprocessError) as exc:
                    error = (
                        exc
                        if isinstance(exc, CFError)
                        else CFError("origin_identity", "项目运行归属读取失败")
                    )
                    self._cleanup_record(key, record, error)
                    continue
                if (
                    not force
                    and self.clock() - record.get("last_probe_attempt", 0) < 30
                ):
                    continue
                self.store.update(key, last_probe_attempt=self.clock())
                try:
                    from app.services.cf_tunnels.runtime import quick_url

                    observed = self._runtime_for(record["handle"]).observe(
                        record["handle"]
                    )
                    if not observed["active"] or not observed.get("url"):
                        raise CFError("cf_connect", "CF 当前连接尚未提供有效网址")
                    public = quick_url(observed["url"]) + target["path"]
                    checked = probe(
                        self.http_transport,
                        public,
                        timeout=self.probe_timeout,
                        stage="public_probe",
                    )
                    if (
                        not checked["reachable"]
                        or checked["fingerprint"] != target["fingerprint"]
                    ):
                        raise CFError(
                            "public_probe",
                            checked.get("reason") or "公网响应与当前项目不一致",
                        )
                    self.store.update(
                        key,
                        state="online",
                        current_url=public,
                        target=target,
                        last_verified_at=datetime.fromtimestamp(
                            self.clock(), UTC
                        ).isoformat(),
                        error=None,
                    )
                except (CFError, httpx.HTTPError, OSError, SubprocessError) as exc:
                    error = (
                        exc
                        if isinstance(exc, CFError)
                        else CFError(
                            "public_probe",
                            http_error_message(exc)
                            if isinstance(exc, httpx.HTTPError)
                            else f"公网核验失败 ({type(exc).__name__})",
                        )
                    )
                    self.store.update(
                        key,
                        state="error",
                        current_url=None,
                        history_url=record.get("current_url")
                        or record.get("history_url"),
                        error=error.as_dict(),
                    )

    def _close_record(self, key, record):
        deadline = monotonic() + 15
        self.store.update(
            key,
            state="stopping",
            current_url=None,
            history_url=record.get("current_url") or record.get("history_url"),
        )
        try:
            handle = record.get("handle")
            if record.get("legacy") and (not handle or not handle.get("invocation_id")):
                handle = self.legacy_runtime.current_handle(
                    key,
                    record["last_task_id"],
                    legacy=record["legacy"],
                    timeout=budget(deadline, stage="cleanup"),
                )
                self.store.update(key, handle=handle)
            if handle:
                self._runtime_for(handle).stop(
                    handle, timeout=budget(deadline, 15, stage="cleanup")
                )
            self.store.update(
                key, state="off", handle=None, error=None, last_verified_at=None
            )
        except Exception as exc:
            error = (
                CFError(exc.stage, f"{exc}；原隧道仍待清理")
                if isinstance(exc, CFError)
                else CFError(
                    "cleanup", f"CF 关闭失败 ({type(exc).__name__})，原隧道仍待清理"
                )
            )
            self.store.update(key, state="error", error=error.as_dict())
            raise error from exc

    def _cleanup_record(self, key, record, error):
        handle = record.get("handle")
        clean = bool(handle) and handle.get("kind") != "legacy"
        if handle and error.stage == "persistence":
            clean = record.get("desired") == "off" or (
                handle.get("token") == record.get("last_task_id")
                and (handle.get("kind") != "legacy" or handle.get("started_by_panel"))
            )
        # Background source maintenance withdraws external URLs, not external units.
        if handle and clean:
            try:
                self._runtime_for(handle).stop(handle, timeout=15)
                handle = None
            except Exception as exc:
                message = str(exc) if isinstance(exc, CFError) else type(exc).__name__
                error = CFError("cleanup", f"{error}；原隧道待清理 ({message})")
        return self.store.update(
            key,
            state="error",
            current_url=None,
            handle=handle,
            history_url=record.get("current_url") or record.get("history_url"),
            error=error.as_dict(),
        )

    def decorate(self, asset: AssetSnapshot) -> AssetSnapshot:
        if asset.category not in {"docker", "project"}:
            return asset
        status = self.status(asset)
        actions = [
            action for action in asset.supports_actions if action not in CF_ACTIONS
        ]
        if status["capable"]:
            actions = list(dict.fromkeys([*actions, *sorted(CF_ACTIONS)]))
        metadata = dict(asset.metadata)
        capabilities = dict(metadata.get("capabilities") or {})
        cf = dict(capabilities.get("cf_tunnel") or {})
        cf.update(
            enabled=status["capable"],
            supported_actions=sorted(CF_ACTIONS) if status["capable"] else [],
            current_url=status["current_url"],
            reason=status.get("reason"),
        )
        capabilities["cf_tunnel"] = cf
        metadata.update(cf=status, capabilities=capabilities)
        return asset.model_copy(
            update={"metadata": metadata, "supports_actions": actions}
        )

    def prepare(self, asset: AssetSnapshot) -> dict[str, object]:
        try:
            legacy = self._legacy(asset)
            prepared = prepare_targets(
                asset,
                self.http_transport,
                timeout=self.prepare_timeout,
                probe_timeout=self.probe_timeout,
                proc_root=self.proc_root,
            )
            if legacy:
                prepared["targets"] = [
                    item
                    for item in prepared["targets"]
                    if item["origin"].rstrip("/") == legacy["origin"].rstrip("/")
                ]
                if not prepared["targets"]:
                    raise CFError(
                        "legacy_origin",
                        "旧隧道源站与当前项目入口不一致，原服务保持原样",
                    )
            prepared["management"] = "external" if legacy else "panel"
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
                "origin_probe",
                http_error_message(exc)
                if isinstance(exc, httpx.HTTPError)
                else f"项目入口读取失败 ({type(exc).__name__})",
            ) from exc

    def enqueue(
        self,
        queue,
        asset: AssetSnapshot,
        action: str,
        choice: TargetChoice | None = None,
    ):
        from app.adapters.base import ActionPlan

        key = self._key(asset) if action == "cf_disable" else owner_key(asset)
        record = self.store.get(key)
        legacy = (
            record.get("legacy")
            if action == "cf_disable" and record
            else self._legacy(asset)
        )
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
            legacy=legacy,
        )
        plan = ActionPlan(
            commands=[], cf_operation=operation, command_timeout_seconds=120
        )
        with exclusive_file_lock(self.data_root / "cf" / f"{key}.enqueue.lock"):
            self.store.accept(operation, boot_id=self.runtime.boot_id)
            try:
                task = queue.enqueue(
                    asset.object_id, action, plan=plan, task_id=operation.request_id
                )
            except Exception as exc:
                self.store.settle_request(operation.request_id, "failed")
                record = self.store.get(key) or {}
                error = CFError("persistence", "CF 任务入队失败，请重试")
                self.store.update(
                    key,
                    state="error",
                    current_url=None,
                    history_url=record.get("current_url") or record.get("history_url"),
                    error=error.as_dict(),
                )
                raise error from exc
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
                try:
                    self._close_record(key, record)
                    self.store.settle_request(operation.request_id, "succeeded")
                    return
                except CFError:
                    self.store.settle_request(operation.request_id, "failed")
                    raise
            self.store.update(key, state="starting", current_url=None, error=None)
            handle = None
            created = False
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
                if (
                    existing
                    and self._runtime_for(existing).observe(
                        existing, timeout=budget(total_deadline)
                    )["active"]
                ):
                    if (
                        operation.action == "cf_create"
                        and record.get("target", {}).get("revision")
                        == target["revision"]
                    ):
                        handle = existing
                    else:
                        self._runtime_for(existing).stop(
                            existing, timeout=budget(total_deadline, 15)
                        )
                        self.store.update(
                            key,
                            handle=None,
                            history_url=record.get("current_url")
                            or record.get("history_url"),
                        )
                if handle is None:
                    self.store.update(
                        key,
                        target=target,
                        active_generation=operation.request_id,
                        launch_pending=True,
                    )
                    runtime = self.legacy_runtime if operation.legacy else self.runtime
                    handle = runtime.start(
                        key,
                        operation.request_id,
                        target["origin"],
                        timeout=budget(total_deadline, 10),
                        **({"legacy": operation.legacy} if operation.legacy else {}),
                    )
                    created = handle.get("kind") != "legacy" or handle.get(
                        "started_by_panel", False
                    )
                self.store.update(
                    key,
                    handle=handle,
                    target=target,
                    active_generation=operation.request_id,
                    launch_pending=False,
                )
                deadline = min(total_deadline, monotonic() + self.public_timeout)
                last_error = CFError("cf_connect", "CF 网址建立超时")
                while monotonic() < deadline:
                    observed = self._runtime_for(handle).observe(
                        handle, timeout=budget(deadline, stage="cf_connect")
                    )
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
                                timeout=budget(
                                    deadline, self.probe_timeout, stage="public_probe"
                                ),
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
                                    last_verified_at=datetime.fromtimestamp(
                                        self.clock(), UTC
                                    ).isoformat(),
                                    last_probe_attempt=self.clock(),
                                    error=None,
                                )
                                self._runtime_for(handle).activate(
                                    handle, timeout=budget(total_deadline)
                                )
                                self.store.settle_request(
                                    operation.request_id, "succeeded"
                                )
                                return
                            last_error = CFError(
                                "public_probe",
                                checked.get("reason") or "公网响应与项目入口不一致",
                            )
                        except CFError as exc:
                            if exc.stage != "public_probe":
                                raise
                            last_error = exc
                        except httpx.HTTPError as exc:
                            last_error = CFError(
                                "public_probe",
                                http_error_message(exc),
                            )
                    sleep(min(0.2, max(0, deadline - monotonic())))
                raise last_error
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, CFError)
                    else CFError("runtime", f"CF 执行失败 ({type(exc).__name__})")
                )
                attached = getattr(exc, "handle", None)
                if attached:
                    created = attached.get("kind") != "legacy" or attached.get(
                        "started_by_panel", False
                    )
                handle = handle or attached
                cleanup = {}
                if handle:
                    if created:
                        try:
                            self._runtime_for(handle).stop(handle)
                            handle = None
                        except Exception:
                            error = CFError("cleanup", f"{error}；隧道仍待清理")
                    cleanup["handle"] = handle
                self.store.update(
                    key,
                    state="error",
                    current_url=None,
                    error=error.as_dict(),
                    launch_pending=False,
                    **cleanup,
                )
                self.store.settle_request(operation.request_id, "failed")
                raise error from exc
