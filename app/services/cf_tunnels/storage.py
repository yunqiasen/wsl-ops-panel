"""CF-owned rows in the existing panel database, separate from discovery caches."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from threading import RLock

from app.services.cf_tunnels.models import CFOperation


class CFStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        # One short transaction per mutation; no network work holds a DB lock.
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.path, timeout=5) as connection:
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS cf_tunnels (owner TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS cf_requests (id TEXT PRIMARY KEY, owner TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL)"
                )
                connection.execute("BEGIN IMMEDIATE")
                yield connection
            self.path.chmod(0o600)

    def get(self, owner: str) -> dict | None:
        if not self.path.exists():
            return None
        with sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, timeout=5
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='cf_tunnels'"
            ).fetchone()
            row = (
                connection.execute(
                    "SELECT payload FROM cf_tunnels WHERE owner=?", (owner,)
                ).fetchone()
                if exists
                else None
            )
        return json.loads(row[0]) if row else None

    def update(self, owner: str, **changes) -> dict:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM cf_tunnels WHERE owner=?", (owner,)
            ).fetchone()
            record = json.loads(row[0]) if row else {}
            record.update(changes)
            connection.execute(
                "INSERT INTO cf_tunnels VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload",
                (owner, json.dumps(record)),
            )
        return record

    def accept(self, operation: CFOperation) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload FROM cf_tunnels WHERE owner=?", (operation.owner,)
            ).fetchone()
            record = (
                json.loads(row[0]) if row else {"state": "off", "current_url": None}
            )
            record.update(
                desired="off" if operation.action == "cf_disable" else "on",
                last_task_id=operation.request_id,
                asset=operation.asset.model_dump(mode="json"),
            )
            connection.execute(
                "INSERT INTO cf_tunnels VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload",
                (operation.owner, json.dumps(record)),
            )
            connection.execute(
                "INSERT INTO cf_requests VALUES (?, ?, ?, 'queued')",
                (operation.request_id, operation.owner, operation.action),
            )

    def pending(self, owner: str) -> list[tuple[str, str]]:
        if not self.path.exists():
            return []
        with sqlite3.connect(
            f"file:{self.path}?mode=ro", uri=True, timeout=5
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='cf_requests'"
            ).fetchone()
            return (
                connection.execute(
                    "SELECT id, action FROM cf_requests WHERE owner=? AND status IN ('queued', 'running') ORDER BY rowid",
                    (owner,),
                ).fetchall()
                if exists
                else []
            )

    def request_status(self, request_id: str) -> str | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status FROM cf_requests WHERE id=?", (request_id,)
            ).fetchone()
        return row[0] if row else None

    def settle_request(self, request_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE cf_requests SET status=? WHERE id=?", (status, request_id)
            )
