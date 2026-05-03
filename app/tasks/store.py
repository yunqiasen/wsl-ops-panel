from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.core.db import connect_sqlite
from app.models.db import SQLiteDatabaseConfig
from app.models.tasks import TaskRecord


class TaskStore(Protocol):
    def insert(self, task: TaskRecord) -> None: ...

    def list_all(self) -> list[TaskRecord]: ...

    def mark_running_as_interrupted(self) -> None: ...


class InMemoryTaskStore:
    def __init__(self, seed_running: bool = False) -> None:
        self._items: list[TaskRecord] = []
        if seed_running:
            self._items.append(
                TaskRecord(
                    id='seed-running-task',
                    object_id='seed-object',
                    action='seed-action',
                    status='running',
                    stdout_log_path='data/operations/pending.log',
                    stderr_log_path='data/operations/pending.err.log',
                    created_at=datetime.now(UTC),
                    started_at=datetime.now(UTC),
                )
            )

    def insert(self, task: TaskRecord) -> None:
        self._items.append(task)

    def list_all(self) -> list[TaskRecord]:
        return [task.model_copy(deep=True) for task in self._items]

    def mark_running_as_interrupted(self) -> None:
        for task in self._items:
            if task.status == 'running':
                task.status = 'interrupted'
                task.finished_at = datetime.now(UTC)


class SQLiteTaskStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        config = SQLiteDatabaseConfig(path=Path(db_path)) if db_path is not None else SQLiteDatabaseConfig()
        self._connection = connect_sqlite(config)

    def insert(self, task: TaskRecord) -> None:
        self._connection.execute(
            '''
            INSERT INTO tasks (
                id,
                object_id,
                action,
                requested_version,
                status,
                stdout_log_path,
                stderr_log_path,
                created_at,
                started_at,
                finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                task.id,
                task.object_id,
                task.action,
                task.requested_version,
                task.status,
                task.stdout_log_path,
                task.stderr_log_path,
                task.created_at.isoformat(),
                task.started_at.isoformat() if task.started_at else None,
                task.finished_at.isoformat() if task.finished_at else None,
            ),
        )
        self._connection.commit()

    def list_all(self) -> list[TaskRecord]:
        rows = self._connection.execute(
            '''
            SELECT
                id,
                object_id,
                action,
                requested_version,
                status,
                stdout_log_path,
                stderr_log_path,
                created_at,
                started_at,
                finished_at
            FROM tasks
            ORDER BY rowid ASC
            '''
        ).fetchall()
        return [TaskRecord.model_validate(dict(row)) for row in rows]

    def mark_running_as_interrupted(self) -> None:
        now = datetime.now(UTC).isoformat()
        self._connection.execute(
            '''
            UPDATE tasks
            SET status = 'interrupted',
                finished_at = COALESCE(finished_at, ?)
            WHERE status = 'running'
            ''',
            (now,),
        )
        self._connection.commit()
