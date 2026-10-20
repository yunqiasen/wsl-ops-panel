"""CF-owned rows in the existing panel database, separate from discovery caches."""

from contextlib import closing, contextmanager
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
            with (
                closing(sqlite3.connect(self.path, timeout=5)) as connection,
                connection,
            ):
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

    def _read_query(self, table: str, query: str, args: tuple = ()) -> list:
        if not self.path.exists():
            return []
        with closing(
            sqlite3.connect(
                self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5
            )
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (table,)
            ).fetchone()
            return connection.execute(query, args).fetchall() if exists else []

    def get(self, owner: str) -> dict | None:
        rows = self._read_query(
            "cf_tunnels", "SELECT payload FROM cf_tunnels WHERE owner=?", (owner,)
        )
        return json.loads(rows[0][0]) if rows else None

    def records(self) -> list[tuple[str, dict]]:
        return [
            (owner, json.loads(payload))
            for owner, payload in self._read_query(
                "cf_tunnels", "SELECT owner, payload FROM cf_tunnels"
            )
        ]

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

    def accept(self, operation: CFOperation, *, boot_id: str | None = None) -> None:
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
                boot_id=boot_id,
            )
            if operation.target:
                record["selection_id"] = operation.target.id
            connection.execute(
                "INSERT INTO cf_tunnels VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload",
                (operation.owner, json.dumps(record)),
            )
            connection.execute(
                "INSERT INTO cf_requests VALUES (?, ?, ?, 'queued')",
                (operation.request_id, operation.owner, operation.action),
            )

    def pending(self, owner: str) -> list[tuple[str, str]]:
        return self._read_query(
            "cf_requests",
            "SELECT id, action FROM cf_requests WHERE owner=? AND status IN ('queued', 'running') ORDER BY rowid",
            (owner,),
        )

    def request_status(self, request_id: str) -> str | None:
        rows = self._read_query(
            "cf_requests", "SELECT status FROM cf_requests WHERE id=?", (request_id,)
        )
        return rows[0][0] if rows else None

    def settle_request(self, request_id: str, status: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "UPDATE cf_requests SET status=? WHERE id=?", (status, request_id)
            )
