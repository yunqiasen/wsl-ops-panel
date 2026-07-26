from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any

from app.models.assets import AssetSnapshot

_DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def resolve_state_db_path(root: Path | str) -> Path:
    path = Path(root)
    if path.suffix in _DB_SUFFIXES:
        return path
    if path.name == "agent":
        return path.parent / "state.db"
    if path.name == "data":
        return path / "state.db"
    if path.name == "config":
        return path.parent / "data" / "state.db"
    return path / "data" / "state.db"


class PanelStateStore:
    def __init__(self, root: Path | str) -> None:
        self.path = resolve_state_db_path(root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            self.path, timeout=30, check_same_thread=False
        )
        self.path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._lock = RLock()
        self._configure()
        self._ensure_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def replace_asset_snapshots(
        self, category_id: str, assets: Iterable[AssetSnapshot]
    ) -> None:
        now = _now()
        rows = [self._asset_row(category_id, asset, now) for asset in assets]
        with self._lock:
            self._connection.execute(
                "DELETE FROM asset_snapshots WHERE category_id = ?", (category_id,)
            )
            self._connection.executemany(
                """
                INSERT INTO asset_snapshots (
                    category_id,
                    object_id,
                    name,
                    status,
                    current_version,
                    latest_version,
                    payload_json,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            self._connection.commit()

    def list_asset_snapshots(self, category_id: str) -> list[AssetSnapshot]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload_json
                FROM asset_snapshots
                WHERE category_id = ?
                ORDER BY name COLLATE NOCASE ASC, object_id ASC
                """,
                (category_id,),
            ).fetchall()
        return [AssetSnapshot.model_validate_json(row["payload_json"]) for row in rows]

    def get_asset_snapshot(self, object_id: str) -> AssetSnapshot | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT payload_json
                FROM asset_snapshots
                WHERE object_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (object_id,),
            ).fetchone()
        return (
            None
            if row is None
            else AssetSnapshot.model_validate_json(row["payload_json"])
        )

    def list_mcp_servers(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, spec_json, description, homepage, docs, tags_json, source, created_at, updated_at
                FROM agent_mcp_servers
                ORDER BY name COLLATE NOCASE ASC, id ASC
                """
            ).fetchall()
            target_rows = self._connection.execute(
                "SELECT server_id, app_id, enabled FROM agent_mcp_targets ORDER BY app_id ASC"
            ).fetchall()
        apps_by_server: dict[str, dict[str, bool]] = {}
        for row in target_rows:
            apps_by_server.setdefault(row["server_id"], {})[row["app_id"]] = bool(
                row["enabled"]
            )
        return {
            row["id"]: {
                "id": row["id"],
                "name": row["name"],
                "spec": json.loads(row["spec_json"] or "{}"),
                "apps": apps_by_server.get(row["id"], {}),
                "description": row["description"],
                "homepage": row["homepage"],
                "docs": row["docs"],
                "tags": json.loads(row["tags_json"] or "[]"),
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def upsert_mcp_server(
        self,
        server_id: str,
        spec: dict[str, Any],
        apps: dict[str, bool] | None = None,
        source: str = "manual",
        *,
        name: str | None = None,
        description: str | None = None,
        homepage: str | None = None,
        docs: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        clean_tags = [str(tag).strip() for tag in tags or [] if str(tag).strip()]
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at FROM agent_mcp_servers WHERE id = ?", (server_id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO agent_mcp_servers (id, name, spec_json, description, homepage, docs, tags_json, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    spec_json = excluded.spec_json,
                    description = excluded.description,
                    homepage = excluded.homepage,
                    docs = excluded.docs,
                    tags_json = excluded.tags_json,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    server_id,
                    name or server_id,
                    json.dumps(spec, ensure_ascii=False, sort_keys=True),
                    description,
                    homepage,
                    docs,
                    json.dumps(clean_tags, ensure_ascii=False),
                    source,
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
            if apps is not None:
                for app_id, enabled in apps.items():
                    self._connection.execute(
                        """
                        INSERT INTO agent_mcp_targets (server_id, app_id, enabled, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(server_id, app_id) DO UPDATE SET
                            enabled = excluded.enabled,
                            updated_at = excluded.updated_at
                        """,
                        (server_id, app_id, 1 if enabled else 0, now),
                    )
            self._connection.commit()
        return self.list_mcp_servers()[server_id]

    def delete_mcp_server(self, server_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_mcp_servers WHERE id = ?", (server_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def upsert_mcp_variant(
        self,
        mcp_id: str,
        client_id: str,
        platform: str,
        spec: dict[str, Any],
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_mcp_variants (mcp_id, client_id, platform, spec_json, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mcp_id, client_id, platform) DO UPDATE SET
                    spec_json = excluded.spec_json, source = excluded.source, updated_at = excluded.updated_at
                """,
                (
                    mcp_id,
                    client_id,
                    platform,
                    json.dumps(spec, ensure_ascii=False, sort_keys=True),
                    source,
                    now,
                    now,
                ),
            )
            self._connection.commit()
        return self.list_mcp_variants(mcp_id, client_id, platform)[0]

    def list_mcp_variants(
        self,
        mcp_id: str | None = None,
        client_id: str | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (
            ("mcp_id", mcp_id),
            ("client_id", client_id),
            ("platform", platform),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_mcp_variants{where} ORDER BY mcp_id, client_id, platform",
                params,
            ).fetchall()
        return [
            {**dict(row), "spec": json.loads(row["spec_json"] or "{}")} for row in rows
        ]

    def replace_mcp_assignments(
        self, node_id: str, client_id: str, assignments: list[dict[str, Any]]
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                "DELETE FROM agent_mcp_assignments WHERE node_id = ? AND client_id = ?",
                (node_id, client_id),
            )
            for item in assignments:
                self._connection.execute(
                    """
                    INSERT INTO agent_mcp_assignments
                    (node_id, client_id, mcp_id, variant_client_id, variant_platform, desired_enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        client_id,
                        item["mcp_id"],
                        item.get("variant_client_id", client_id),
                        item.get("variant_platform", "any"),
                        1 if item.get("desired_enabled", True) else 0,
                        now,
                    ),
                )
            self._connection.commit()

    def remove_mcp_assignments(
        self, node_id: str, client_id: str, mcp_ids: set[str]
    ) -> int:
        selected = sorted({mcp_id for mcp_id in mcp_ids if mcp_id})
        if not selected:
            return 0
        placeholders = ", ".join("?" for _ in selected)
        with self._lock:
            cursor = self._connection.execute(
                f"DELETE FROM agent_mcp_assignments WHERE node_id = ? AND client_id = ? AND mcp_id IN ({placeholders})",
                [node_id, client_id, *selected],
            )
            self._connection.commit()
            return cursor.rowcount

    def list_mcp_assignments(
        self, node_id: str | None = None, client_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if client_id is not None:
            clauses.append("client_id = ?")
            params.append(client_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_mcp_assignments{where} ORDER BY node_id, client_id, mcp_id",
                params,
            ).fetchall()
        return [
            {**dict(row), "desired_enabled": bool(row["desired_enabled"])}
            for row in rows
        ]

    def replace_mcp_observations(
        self, node_id: str, client_id: str, observations: list[dict[str, Any]]
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                "DELETE FROM agent_mcp_observations WHERE node_id = ? AND client_id = ?",
                (node_id, client_id),
            )
            for item in observations:
                self._connection.execute(
                    """
                    INSERT INTO agent_mcp_observations
                    (node_id, client_id, mcp_id, present, spec_hash, public_spec_json, status, scanned_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        client_id,
                        item["mcp_id"],
                        1 if item.get("present", True) else 0,
                        item.get("spec_hash"),
                        json.dumps(
                            item.get("public_spec") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        item.get("status", "unknown"),
                        item.get("scanned_at", now),
                        item.get("error"),
                    ),
                )
            self._connection.commit()

    def list_mcp_observations(
        self, node_id: str | None = None, client_id: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if client_id is not None:
            clauses.append("client_id = ?")
            params.append(client_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_mcp_observations{where} ORDER BY node_id, client_id, mcp_id",
                params,
            ).fetchall()
        return [
            {
                **dict(row),
                "present": bool(row["present"]),
                "public_spec": json.loads(row["public_spec_json"] or "{}"),
            }
            for row in rows
        ]

    def record_mcp_operation(
        self,
        operation_id: str,
        node_id: str,
        client_id: str,
        action: str,
        status: str,
        *,
        task_id: str | None = None,
        error: str | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """INSERT INTO agent_mcp_operations
                (id, node_id, client_id, action, status, task_id, error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status, task_id=excluded.task_id, error=excluded.error, updated_at=excluded.updated_at""",
                (
                    operation_id,
                    node_id,
                    client_id,
                    action,
                    status,
                    task_id,
                    error,
                    now,
                    now,
                ),
            )
            self._connection.commit()

    def list_mcp_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM agent_mcp_operations ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_prompts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, content, created_at, updated_at
                FROM agent_prompts
                ORDER BY name COLLATE NOCASE ASC, id ASC
                """
            ).fetchall()
        return {
            row["id"]: {
                "id": row["id"],
                "name": row["name"],
                "content": row["content"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def upsert_prompt(self, prompt_id: str, name: str, content: str) -> dict[str, Any]:
        now = _now()
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at FROM agent_prompts WHERE id = ?", (prompt_id,)
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO agent_prompts (id, name, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (
                    prompt_id,
                    name,
                    content,
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
            self._connection.commit()
        return self.list_prompts()[prompt_id]

    def delete_prompt(self, prompt_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_prompts WHERE id = ?", (prompt_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def list_agent_providers(
        self, app_id: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        params: tuple[str, ...] = (app_id,) if app_id else ()
        where = "WHERE app_id = ?" if app_id else ""
        with self._lock:
            rows = self._connection.execute(
                f"""
                SELECT
                    id,
                    app_id,
                    name,
                    settings_json,
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    is_current,
                    source,
                    created_at,
                    updated_at
                FROM agent_providers
                {where}
                ORDER BY app_id ASC, is_current DESC, sort_index ASC, name COLLATE NOCASE ASC
                """,
                params,
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["app_id"], []).append(_provider_from_row(row))
        return grouped

    def get_agent_provider(
        self, app_id: str, provider_id: str
    ) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT
                    id,
                    app_id,
                    name,
                    settings_json,
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    is_current,
                    source,
                    created_at,
                    updated_at
                FROM agent_providers
                WHERE app_id = ? AND id = ?
                """,
                (app_id, provider_id),
            ).fetchone()
        return None if row is None else _provider_from_row(row)

    def upsert_agent_provider(
        self,
        *,
        app_id: str,
        provider_id: str,
        name: str,
        settings: dict[str, Any],
        website_url: str | None = None,
        category: str | None = None,
        notes: str | None = None,
        icon: str | None = None,
        icon_color: str | None = None,
        is_current: bool = False,
        source: str = "manual",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at FROM agent_providers WHERE app_id = ? AND id = ?",
                (app_id, provider_id),
            ).fetchone()
            if is_current:
                self._connection.execute(
                    "UPDATE agent_providers SET is_current = 0 WHERE app_id = ?",
                    (app_id,),
                )
            self._connection.execute(
                """
                INSERT INTO agent_providers (
                    id,
                    app_id,
                    name,
                    settings_json,
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    is_current,
                    source,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, app_id) DO UPDATE SET
                    name = excluded.name,
                    settings_json = excluded.settings_json,
                    website_url = excluded.website_url,
                    category = excluded.category,
                    notes = excluded.notes,
                    icon = excluded.icon,
                    icon_color = excluded.icon_color,
                    is_current = excluded.is_current,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    provider_id,
                    app_id,
                    name,
                    json.dumps(settings, ensure_ascii=False, sort_keys=True),
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    1 if is_current else 0,
                    source,
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
            self._connection.commit()
        provider = self.get_agent_provider(app_id, provider_id)
        if provider is None:
            raise KeyError(f"provider not found after upsert: {app_id}/{provider_id}")
        return provider

    def set_current_agent_provider(self, app_id: str, provider_id: str) -> bool:
        now = _now()
        with self._lock:
            exists = self._connection.execute(
                "SELECT 1 FROM agent_providers WHERE app_id = ? AND id = ?",
                (app_id, provider_id),
            ).fetchone()
            if exists is None:
                return False
            self._connection.execute(
                "UPDATE agent_providers SET is_current = 0 WHERE app_id = ?", (app_id,)
            )
            self._connection.execute(
                "UPDATE agent_providers SET is_current = 1, updated_at = ? WHERE app_id = ? AND id = ?",
                (now, app_id, provider_id),
            )
            self._connection.commit()
        return True

    def delete_agent_provider(self, app_id: str, provider_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_providers WHERE app_id = ? AND id = ?",
                (app_id, provider_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def _configure(self) -> None:
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def _ensure_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS asset_snapshots (
                    category_id TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_version TEXT,
                    latest_version TEXT,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (category_id, object_id)
                );

                CREATE INDEX IF NOT EXISTS idx_asset_snapshots_object_id
                ON asset_snapshots(object_id);

                CREATE TABLE IF NOT EXISTS agent_mcp_servers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    description TEXT,
                    homepage TEXT,
                    docs TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_mcp_targets (
                    server_id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (server_id, app_id),
                    FOREIGN KEY (server_id) REFERENCES agent_mcp_servers(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_mcp_variants (
                    mcp_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (mcp_id, client_id, platform),
                    FOREIGN KEY (mcp_id) REFERENCES agent_mcp_servers(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_mcp_assignments (
                    node_id TEXT NOT NULL, client_id TEXT NOT NULL, mcp_id TEXT NOT NULL,
                    variant_client_id TEXT NOT NULL, variant_platform TEXT NOT NULL,
                    desired_enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_id, client_id, mcp_id),
                    FOREIGN KEY (mcp_id) REFERENCES agent_mcp_servers(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_mcp_observations (
                    node_id TEXT NOT NULL, client_id TEXT NOT NULL, mcp_id TEXT NOT NULL,
                    present INTEGER NOT NULL, spec_hash TEXT, public_spec_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL, scanned_at TEXT NOT NULL, error TEXT,
                    PRIMARY KEY (node_id, client_id, mcp_id)
                );

                CREATE TABLE IF NOT EXISTS agent_mcp_operations (
                    id TEXT PRIMARY KEY, node_id TEXT NOT NULL, client_id TEXT NOT NULL,
                    action TEXT NOT NULL, status TEXT NOT NULL, task_id TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_prompts (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_providers (
                    id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    website_url TEXT,
                    category TEXT,
                    notes TEXT,
                    icon TEXT,
                    icon_color TEXT,
                    is_current INTEGER NOT NULL DEFAULT 0,
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (id, app_id)
                );

                CREATE INDEX IF NOT EXISTS idx_agent_providers_app
                ON agent_providers(app_id, is_current, name);

                CREATE TABLE IF NOT EXISTS asset_settings (
                    object_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (object_id, key)
                );
                """
            )
            for column, ddl in (
                (
                    "description",
                    "ALTER TABLE agent_mcp_servers ADD COLUMN description TEXT",
                ),
                ("homepage", "ALTER TABLE agent_mcp_servers ADD COLUMN homepage TEXT"),
                ("docs", "ALTER TABLE agent_mcp_servers ADD COLUMN docs TEXT"),
                (
                    "tags_json",
                    "ALTER TABLE agent_mcp_servers ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'",
                ),
            ):
                self._add_column_if_missing("agent_mcp_servers", column, ddl)
            self._connection.commit()

    def _add_column_if_missing(self, table: str, column: str, ddl: str) -> None:
        columns = {
            row["name"]
            for row in self._connection.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }
        if column not in columns:
            self._connection.execute(ddl)

    @staticmethod
    def _asset_row(
        category_id: str, asset: AssetSnapshot, updated_at: str
    ) -> tuple[str, str, str, str, str | None, str | None, str, str]:
        return (
            category_id,
            asset.object_id,
            asset.name,
            asset.status,
            asset.current_version,
            asset.latest_version,
            asset.model_dump_json(),
            updated_at,
        )


def _provider_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "app_id": row["app_id"],
        "name": row["name"],
        "settings_config": json.loads(row["settings_json"] or "{}"),
        "website_url": row["website_url"],
        "category": row["category"],
        "notes": row["notes"],
        "icon": row["icon"],
        "icon_color": row["icon_color"],
        "is_current": bool(row["is_current"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()
