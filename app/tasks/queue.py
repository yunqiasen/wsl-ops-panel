from datetime import UTC, datetime
from uuid import uuid4

from app.models.tasks import TaskRecord
from app.tasks.store import TaskStore


class GlobalTaskQueue:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def enqueue(self, object_id: str, action: str, requested_version: str | None = None) -> TaskRecord:
        task = TaskRecord(
            id=str(uuid4()),
            object_id=object_id,
            action=action,
            requested_version=requested_version,
            status='queued',
            stdout_log_path='data/operations/pending.log',
            stderr_log_path='data/operations/pending.err.log',
            created_at=datetime.now(UTC),
        )
        self.store.insert(task)
        return task

    def recover_on_startup(self) -> None:
        self.store.mark_running_as_interrupted()
