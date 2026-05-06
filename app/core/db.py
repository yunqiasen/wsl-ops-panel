import sqlite3
from pathlib import Path

from app.models.db import SQLiteDatabaseConfig

_TASKS_SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    requested_version TEXT,
    status TEXT NOT NULL,
    stdout_log_path TEXT NOT NULL,
    stderr_log_path TEXT NOT NULL,
    plan_path TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
)
'''
_OPTIONAL_TASK_COLUMNS = {
    'plan_path': 'TEXT',
}


def connect_sqlite(config: SQLiteDatabaseConfig | None = None) -> sqlite3.Connection:
    resolved = config or SQLiteDatabaseConfig()
    db_path = Path(resolved.path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    ensure_task_table(connection)
    return connection


def ensure_task_table(connection: sqlite3.Connection) -> None:
    connection.execute(_TASKS_SCHEMA)
    existing_columns = {
        row['name'] if isinstance(row, sqlite3.Row) else row[1]
        for row in connection.execute('PRAGMA table_info(tasks)').fetchall()
    }
    for column_name, column_type in _OPTIONAL_TASK_COLUMNS.items():
        if column_name in existing_columns:
            continue
        connection.execute(f'ALTER TABLE tasks ADD COLUMN {column_name} {column_type}')
    connection.commit()
