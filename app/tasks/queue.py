from collections.abc import Callable
from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

from app.adapters.base import ActionPlan
from app.models.tasks import TaskRecord
from app.tasks.store import TaskStore


class GlobalTaskQueue:
    def __init__(self, store: TaskStore, *, operations_root: Path | str = Path('data/operations')) -> None:
        self.store = store
        self.operations_root = Path(operations_root).resolve()
        self._enqueue_notifier: Callable[[], None] | None = None

    def set_enqueue_notifier(self, notifier: Callable[[], None]) -> None:
        self._enqueue_notifier = notifier

    def peek_next_queued_task(self) -> TaskRecord | None:
        return self.store.get_first_queued()

    def enqueue(
        self,
        object_id: str,
        action: str,
        requested_version: str | None = None,
        *,
        plan: ActionPlan | None = None,
    ) -> TaskRecord:
        task_id = str(uuid4())
        operation_dir = self.operations_root / task_id
        operation_dir.mkdir(parents=True, exist_ok=True)
        operation_dir.chmod(0o700)
        plan_path = operation_dir / "plan.json"
        task = TaskRecord(
            id=task_id,
            object_id=object_id,
            action=action,
            requested_version=requested_version,
            status="queued",
            stdout_log_path=str(operation_dir / "stdout.log"),
            stderr_log_path=str(operation_dir / "stderr.log"),
            plan_path=str(plan_path),
            created_at=datetime.now(UTC),
        )
        if plan is not None:
            plan_path.write_text(
                json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            plan_path.chmod(0o600)
        self.store.insert(task)
        if self._enqueue_notifier is not None:
            self._enqueue_notifier()
        return task

    def recover_on_startup(self) -> None:
        self.store.mark_running_as_interrupted()
