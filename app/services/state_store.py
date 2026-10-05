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

    def list_table_names(self) -> set[str]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        return {str(row["name"]) for row in rows}

    def get_agent_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM agent_settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            return default

    def set_agent_setting(self, key: str, value: Any) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True), now),
            )
            self._connection.commit()

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
                SELECT id, name, spec_json, description, homepage, docs, tags_json,
                       source, sort_index, created_at, updated_at
                FROM agent_mcp_servers
                ORDER BY sort_index ASC, name COLLATE NOCASE ASC, id ASC
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
                "sort_index": int(row["sort_index"]),
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
                "SELECT created_at, sort_index FROM agent_mcp_servers WHERE id = ?",
                (server_id,),
            ).fetchone()
            sort_index = (
                int(existing["sort_index"])
                if existing
                else self._next_sort_index("agent_mcp_servers")
            )
            self._connection.execute(
                """
                INSERT INTO agent_mcp_servers
                (id, name, spec_json, description, homepage, docs, tags_json,
                 source, sort_index, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    sort_index,
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

    def set_mcp_assignment(
        self,
        node_id: str,
        client_id: str,
        mcp_id: str,
        *,
        variant_client_id: str | None = None,
        variant_platform: str = "any",
        desired_enabled: bool = True,
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_mcp_assignments
                (node_id, client_id, mcp_id, variant_client_id, variant_platform, desired_enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, client_id, mcp_id) DO UPDATE SET
                    variant_client_id = excluded.variant_client_id,
                    variant_platform = excluded.variant_platform,
                    desired_enabled = excluded.desired_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    client_id,
                    mcp_id,
                    variant_client_id or client_id,
                    variant_platform,
                    1 if desired_enabled else 0,
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
        self, node_id: str, client_id: str, observations: list[dict[str, Any]],
        *, scan_error: str | None = None,
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
            scan_state = {"status": "error" if scan_error else "ok", "error": scan_error, "scanned_at": now}
            self._connection.execute(
                "INSERT INTO agent_settings (key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (self._mcp_scan_key(node_id, client_id), json.dumps(scan_state), now),
            )
            self._connection.commit()

    @staticmethod
    def _mcp_scan_key(node_id: str, client_id: str) -> str:
        return 'mcp_scan:' + json.dumps([node_id, client_id])

    def get_mcp_scan_state(self, node_id: str, client_id: str) -> dict[str, Any]:
        return self.get_agent_setting(self._mcp_scan_key(node_id, client_id), {})

    def record_mcp_scan_error(self, node_id: str, client_id: str, error: str) -> None:
        rows = {row['mcp_id']: row for row in self.list_mcp_observations(node_id, client_id)}
        for assignment in self.list_mcp_assignments(node_id, client_id):
            rows.setdefault(assignment['mcp_id'], {'mcp_id': assignment['mcp_id'], 'present': False})
        observations = [
            {**row, 'status': 'error', 'error': error, 'scanned_at': _now()}
            for row in rows.values()
        ]
        self.replace_mcp_observations(node_id, client_id, observations, scan_error=error)

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

    def list_skills(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM agent_skills
                ORDER BY sort_index, name COLLATE NOCASE, id
                """
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            result[str(row["id"])] = {
                **dict(row),
                "metadata": metadata if isinstance(metadata, dict) else {},
            }
        return result

    def upsert_skill(
        self,
        *,
        skill_id: str,
        name: str,
        source_kind: str,
        ssot_path: str,
        content_hash: str,
        description: str | None = None,
        source: str | None = None,
        version: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at, sort_index FROM agent_skills WHERE id = ?",
                (skill_id,),
            ).fetchone()
            sort_index = (
                int(existing["sort_index"])
                if existing
                else self._next_sort_index("agent_skills")
            )
            self._connection.execute(
                """
                INSERT INTO agent_skills
                (id, name, description, source, source_kind, ssot_path, version,
                 content_hash, metadata_json, sort_index, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    source = excluded.source,
                    source_kind = excluded.source_kind,
                    ssot_path = excluded.ssot_path,
                    version = excluded.version,
                    content_hash = excluded.content_hash,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    skill_id,
                    name,
                    description,
                    source,
                    source_kind,
                    ssot_path,
                    version,
                    content_hash,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    sort_index,
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
            self._connection.commit()
        return self.list_skills()[skill_id]

    def delete_skill(self, skill_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_skills WHERE id = ?", (skill_id,)
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def upsert_skill_variant(
        self,
        skill_id: str,
        client_id: str,
        platform: str,
        install: dict[str, Any],
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_skill_variants
                (skill_id, client_id, platform, install_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(skill_id, client_id, platform) DO UPDATE SET
                    install_json = excluded.install_json,
                    updated_at = excluded.updated_at
                """,
                (
                    skill_id,
                    client_id,
                    platform,
                    json.dumps(install, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            self._connection.commit()
        return self.list_skill_variants(skill_id, client_id, platform)[0]

    def list_skill_variants(
        self,
        skill_id: str | None = None,
        client_id: str | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (
            ("skill_id", skill_id),
            ("client_id", client_id),
            ("platform", platform),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_skill_variants{where} ORDER BY skill_id, client_id, platform",
                params,
            ).fetchall()
        return [
            {
                **dict(row),
                "install": json.loads(row["install_json"] or "{}"),
            }
            for row in rows
        ]

    def set_skill_assignment(
        self,
        node_id: str,
        client_id: str,
        skill_id: str,
        *,
        variant_client_id: str | None = None,
        variant_platform: str = "linux",
        desired_enabled: bool = True,
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_skill_assignments
                (node_id, client_id, skill_id, variant_client_id, variant_platform, desired_enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, client_id, skill_id) DO UPDATE SET
                    variant_client_id = excluded.variant_client_id,
                    variant_platform = excluded.variant_platform,
                    desired_enabled = excluded.desired_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    client_id,
                    skill_id,
                    variant_client_id or client_id,
                    variant_platform,
                    1 if desired_enabled else 0,
                    now,
                ),
            )
            self._connection.commit()

    def replace_skill_assignments(
        self, node_id: str, client_id: str, assignments: list[dict[str, Any]]
    ) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM agent_skill_assignments WHERE node_id = ? AND client_id = ?",
                (node_id, client_id),
            )
            now = _now()
            for item in assignments:
                self._connection.execute(
                    """
                    INSERT INTO agent_skill_assignments
                    (node_id, client_id, skill_id, variant_client_id, variant_platform, desired_enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        client_id,
                        item["skill_id"],
                        item.get("variant_client_id", client_id),
                        item.get("variant_platform", "linux"),
                        1 if item.get("desired_enabled", True) else 0,
                        now,
                    ),
                )
            self._connection.commit()

    def list_skill_assignments(
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
                f"SELECT * FROM agent_skill_assignments{where} ORDER BY node_id, client_id, skill_id",
                params,
            ).fetchall()
        return [
            {**dict(row), "desired_enabled": bool(row["desired_enabled"])}
            for row in rows
        ]

    def remove_skill_assignments(
        self, node_id: str, client_id: str, skill_ids: set[str]
    ) -> int:
        selected = sorted({skill_id for skill_id in skill_ids if skill_id})
        if not selected:
            return 0
        placeholders = ", ".join("?" for _ in selected)
        with self._lock:
            cursor = self._connection.execute(
                f"DELETE FROM agent_skill_assignments WHERE node_id = ? AND client_id = ? AND skill_id IN ({placeholders})",
                [node_id, client_id, *selected],
            )
            self._connection.commit()
            return cursor.rowcount

    def replace_skill_observations(
        self, node_id: str, client_id: str, observations: list[dict[str, Any]]
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                "DELETE FROM agent_skill_observations WHERE node_id = ? AND client_id = ?",
                (node_id, client_id),
            )
            for item in observations:
                self._connection.execute(
                    """
                    INSERT INTO agent_skill_observations
                    (node_id, client_id, skill_id, present, content_hash, status, scanned_at, error)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        client_id,
                        item["skill_id"],
                        1 if item.get("present", True) else 0,
                        item.get("content_hash"),
                        item.get("status", "unknown"),
                        item.get("scanned_at", now),
                        item.get("error"),
                    ),
                )
            self._connection.commit()

    def upsert_skill_observation(
        self,
        node_id: str,
        client_id: str,
        skill_id: str,
        *,
        present: bool,
        content_hash: str | None,
        status: str,
        error: str | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_skill_observations
                (node_id, client_id, skill_id, present, content_hash, status, scanned_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, client_id, skill_id) DO UPDATE SET
                    present = excluded.present,
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    scanned_at = excluded.scanned_at,
                    error = excluded.error
                """,
                (
                    node_id,
                    client_id,
                    skill_id,
                    1 if present else 0,
                    content_hash,
                    status,
                    now,
                    error,
                ),
            )
            self._connection.commit()

    def list_skill_observations(
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
                f"SELECT * FROM agent_skill_observations{where} ORDER BY node_id, client_id, skill_id",
                params,
            ).fetchall()
        return [{**dict(row), "present": bool(row["present"])} for row in rows]

    def remove_skill_observations(
        self, node_id: str, client_id: str, skill_ids: set[str]
    ) -> int:
        selected = sorted({skill_id for skill_id in skill_ids if skill_id})
        if not selected:
            return 0
        placeholders = ", ".join("?" for _ in selected)
        with self._lock:
            cursor = self._connection.execute(
                f"DELETE FROM agent_skill_observations WHERE node_id = ? AND client_id = ? AND skill_id IN ({placeholders})",
                [node_id, client_id, *selected],
            )
            self._connection.commit()
            return cursor.rowcount

    def list_prompts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, name, description, content, sort_index, created_at, updated_at
                FROM agent_prompts
                ORDER BY sort_index ASC, name COLLATE NOCASE ASC, id ASC
                """
            ).fetchall()
        return {
            row["id"]: {
                "id": row["id"],
                "name": row["name"],
                "description": row["description"],
                "content": row["content"],
                "sort_index": int(row["sort_index"]),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def upsert_prompt(
        self,
        prompt_id: str,
        name: str,
        content: str,
        *,
        description: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at, sort_index FROM agent_prompts WHERE id = ?",
                (prompt_id,),
            ).fetchone()
            sort_index = (
                int(existing["sort_index"])
                if existing
                else self._next_sort_index("agent_prompts")
            )
            self._connection.execute(
                """
                INSERT INTO agent_prompts
                (id, name, description, content, sort_index, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (
                    prompt_id,
                    name,
                    description,
                    content,
                    sort_index,
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

    def upsert_prompt_variant(
        self,
        prompt_id: str,
        client_id: str,
        platform: str,
        content: str,
        *,
        source: str = "manual",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_prompt_variants
                (prompt_id, client_id, platform, content, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prompt_id, client_id, platform) DO UPDATE SET
                    content = excluded.content,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (prompt_id, client_id, platform, content, source, now, now),
            )
            self._connection.commit()
        return self.list_prompt_variants(prompt_id, client_id, platform)[0]

    def list_prompt_variants(
        self,
        prompt_id: str | None = None,
        client_id: str | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        for column, value in (
            ("prompt_id", prompt_id),
            ("client_id", client_id),
            ("platform", platform),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM agent_prompt_variants{where} ORDER BY prompt_id, client_id, platform",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_prompt_assignment(
        self,
        node_id: str,
        client_id: str,
        prompt_id: str,
        *,
        variant_client_id: str | None = None,
        variant_platform: str = "linux",
        desired_enabled: bool = True,
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_prompt_assignments
                (node_id, client_id, prompt_id, variant_client_id, variant_platform, desired_enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, client_id) DO UPDATE SET
                    prompt_id = excluded.prompt_id,
                    variant_client_id = excluded.variant_client_id,
                    variant_platform = excluded.variant_platform,
                    desired_enabled = excluded.desired_enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    client_id,
                    prompt_id,
                    variant_client_id or client_id,
                    variant_platform,
                    1 if desired_enabled else 0,
                    now,
                ),
            )
            self._connection.commit()

    def list_prompt_assignments(
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
                f"SELECT * FROM agent_prompt_assignments{where} ORDER BY node_id, client_id",
                params,
            ).fetchall()
        return [
            {**dict(row), "desired_enabled": bool(row["desired_enabled"])}
            for row in rows
        ]

    def remove_prompt_assignment(self, node_id: str, client_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_prompt_assignments WHERE node_id = ? AND client_id = ?",
                (node_id, client_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def upsert_prompt_observation(
        self,
        node_id: str,
        client_id: str,
        prompt_id: str,
        *,
        present: bool,
        content_hash: str | None,
        status: str,
        error: str | None = None,
    ) -> None:
        now = _now()
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_prompt_observations
                (node_id, client_id, prompt_id, present, content_hash, status, scanned_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, client_id, prompt_id) DO UPDATE SET
                    present = excluded.present,
                    content_hash = excluded.content_hash,
                    status = excluded.status,
                    scanned_at = excluded.scanned_at,
                    error = excluded.error
                """,
                (
                    node_id,
                    client_id,
                    prompt_id,
                    1 if present else 0,
                    content_hash,
                    status,
                    now,
                    error,
                ),
            )
            self._connection.commit()

    def list_prompt_observations(
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
                f"SELECT * FROM agent_prompt_observations{where} ORDER BY node_id, client_id, prompt_id",
                params,
            ).fetchall()
        return [{**dict(row), "present": bool(row["present"])} for row in rows]

    def clear_prompt_observations(self, node_id: str, client_id: str) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_prompt_observations WHERE node_id = ? AND client_id = ?",
                (node_id, client_id),
            )
            self._connection.commit()
            return cursor.rowcount

    def get_router_snapshot(self, node_id: str = "__local__") -> dict[str, Any]:
        with self._lock:
            node = self._connection.execute(
                "SELECT * FROM agent_router_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node is None:
                return {}
            client_rows = self._connection.execute(
                "SELECT * FROM agent_router_clients WHERE node_id = ? ORDER BY client_id",
                (node_id,),
            ).fetchall()
            queue_rows = self._connection.execute(
                """
                SELECT client_id, provider_id, sort_index
                FROM agent_route_failover_queue
                WHERE node_id = ?
                ORDER BY client_id, sort_index, provider_id
                """,
                (node_id,),
            ).fetchall()

        providers: dict[str, dict[str, Any]] = {}
        provider_ids: dict[str, str] = {}
        takeover: dict[str, bool] = {}
        for row in client_rows:
            try:
                profile = json.loads(row["provider_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                profile = {}
            if not isinstance(profile, dict):
                profile = {}
            if profile:
                providers[str(row["client_id"])] = profile
            if row["provider_id"]:
                provider_ids[str(row["client_id"])] = str(row["provider_id"])
            if bool(row["takeover_enabled"]):
                takeover[str(row["client_id"])] = True

        failover_queues: dict[str, list[str]] = {}
        for row in queue_rows:
            failover_queues.setdefault(str(row["client_id"]), []).append(
                str(row["provider_id"])
            )

        return {
            "schema_version": int(node["schema_version"]),
            "listen_address": str(node["listen_address"]),
            "listen_port": int(node["listen_port"]),
            "show_home_switch": bool(node["show_home_switch"]),
            "outbound_proxy": node["outbound_proxy"],
            "providers": providers,
            "provider_ids": provider_ids,
            "takeover": takeover,
            "failover_queues": failover_queues,
            "updated_at": node["updated_at"],
        }

    def replace_router_snapshot(
        self, node_id: str, payload: dict[str, Any]
    ) -> None:
        now = _now()
        providers = payload.get("providers")
        providers = providers if isinstance(providers, dict) else {}
        provider_ids = payload.get("provider_ids")
        provider_ids = provider_ids if isinstance(provider_ids, dict) else {}
        takeover = payload.get("takeover")
        takeover = takeover if isinstance(takeover, dict) else {}
        failover_queues = payload.get("failover_queues")
        failover_queues = failover_queues if isinstance(failover_queues, dict) else {}
        clients = sorted(
            {str(key) for key in providers}
            | {str(key) for key in provider_ids}
            | {str(key) for key in takeover}
            | {str(key) for key in failover_queues}
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO agent_router_nodes
                (node_id, schema_version, listen_address, listen_port, show_home_switch, outbound_proxy, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    listen_address = excluded.listen_address,
                    listen_port = excluded.listen_port,
                    show_home_switch = excluded.show_home_switch,
                    outbound_proxy = excluded.outbound_proxy,
                    updated_at = excluded.updated_at
                """,
                (
                    node_id,
                    int(payload.get("schema_version", 1)),
                    str(payload.get("listen_address") or "127.0.0.1"),
                    int(payload.get("listen_port", 7888)),
                    1 if payload.get("show_home_switch", True) else 0,
                    payload.get("outbound_proxy"),
                    now,
                ),
            )
            self._connection.execute(
                "DELETE FROM agent_route_failover_queue WHERE node_id = ?",
                (node_id,),
            )
            self._connection.execute(
                "DELETE FROM agent_router_clients WHERE node_id = ?", (node_id,)
            )
            for client_id in clients:
                raw_profile = providers.get(client_id)
                profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
                self._connection.execute(
                    """
                    INSERT INTO agent_router_clients
                    (node_id, client_id, provider_id, provider_json, enabled, takeover_enabled,
                     auto_failover, max_retries, failure_threshold, cooldown_seconds, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        client_id,
                        str(provider_ids.get(client_id) or "") or None,
                        json.dumps(profile, ensure_ascii=False, sort_keys=True),
                        1 if profile else 0,
                        1 if takeover.get(client_id) else 0,
                        1 if profile.get("auto_failover", False) else 0,
                        int(profile.get("max_retries", 0)),
                        int(profile.get("failure_threshold", 3)),
                        int(profile.get("cooldown_seconds", 60)),
                        now,
                    ),
                )
                raw_queue = failover_queues.get(client_id)
                queue = raw_queue if isinstance(raw_queue, list) else []
                seen: set[str] = set()
                for sort_index, raw_provider_id in enumerate(queue):
                    queue_provider_id = str(raw_provider_id).strip()
                    if not queue_provider_id or queue_provider_id in seen:
                        continue
                    seen.add(queue_provider_id)
                    self._connection.execute(
                        """
                        INSERT INTO agent_route_failover_queue
                        (node_id, client_id, provider_id, sort_index)
                        VALUES (?, ?, ?, ?)
                        """,
                        (node_id, client_id, queue_provider_id, sort_index),
                    )
            self._connection.commit()

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM agent_profiles ORDER BY sort_index, name COLLATE NOCASE, id"
            ).fetchall()
            item_rows = self._connection.execute(
                """
                SELECT * FROM agent_profile_items
                ORDER BY profile_id, client_id, resource_type, sort_index, resource_id
                """
            ).fetchall()
        items_by_profile: dict[str, list[dict[str, Any]]] = {}
        for row in item_rows:
            try:
                config = json.loads(row["config_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            items_by_profile.setdefault(str(row["profile_id"]), []).append(
                {
                    "client_id": row["client_id"],
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "config": config if isinstance(config, dict) else {},
                    "sort_index": int(row["sort_index"]),
                }
            )
        return [
            {
                **dict(row),
                "items": items_by_profile.get(str(row["id"]), []),
            }
            for row in rows
        ]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        return next(
            (profile for profile in self.list_profiles() if profile["id"] == profile_id),
            None,
        )

    def upsert_profile(
        self,
        profile_id: str,
        name: str,
        description: str | None,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at, sort_index FROM agent_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()
            sort_index = (
                int(existing["sort_index"])
                if existing
                else self._next_sort_index("agent_profiles")
            )
            self._connection.execute(
                """
                INSERT INTO agent_profiles
                (id, name, description, sort_index, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    updated_at = excluded.updated_at
                """,
                (
                    profile_id,
                    name,
                    description,
                    sort_index,
                    existing["created_at"] if existing else now,
                    now,
                ),
            )
            self._connection.execute(
                "DELETE FROM agent_profile_items WHERE profile_id = ?", (profile_id,)
            )
            for index, item in enumerate(items):
                self._connection.execute(
                    """
                    INSERT INTO agent_profile_items
                    (profile_id, client_id, resource_type, resource_id, config_json, sort_index)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        profile_id,
                        str(item["client_id"]),
                        str(item["resource_type"]),
                        str(item["resource_id"]),
                        json.dumps(
                            item.get("config") or {},
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        int(item.get("sort_index", index)),
                    ),
                )
            self._connection.commit()
        profile = self.get_profile(profile_id)
        if profile is None:
            raise KeyError(f"profile not found after upsert: {profile_id}")
        return profile

    def delete_profile(self, profile_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_profiles WHERE id = ?", (profile_id,)
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
                    meta_json,
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    is_current,
                    sort_index,
                    source,
                    created_at,
                    updated_at
                FROM agent_providers
                {where}
                ORDER BY app_id ASC, sort_index ASC, name COLLATE NOCASE ASC, id ASC
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
                    meta_json,
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    is_current,
                    sort_index,
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
        meta: dict[str, Any] | None = None,
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
                "SELECT created_at, sort_index, meta_json FROM agent_providers WHERE app_id = ? AND id = ?",
                (app_id, provider_id),
            ).fetchone()
            sort_index = (
                int(existing["sort_index"])
                if existing
                else self._next_sort_index("agent_providers", app_id=app_id)
            )
            stored_meta = (
                json.loads(existing["meta_json"] or "{}")
                if meta is None and existing is not None
                else dict(meta or {})
            )
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
                    meta_json,
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    is_current,
                    sort_index,
                    source,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id, app_id) DO UPDATE SET
                    name = excluded.name,
                    settings_json = excluded.settings_json,
                    meta_json = excluded.meta_json,
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
                    json.dumps(stored_meta, ensure_ascii=False, sort_keys=True),
                    website_url,
                    category,
                    notes,
                    icon,
                    icon_color,
                    1 if is_current else 0,
                    sort_index,
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

    def clear_current_agent_provider(self, app_id: str, provider_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE agent_providers
                SET is_current = 0, updated_at = ?
                WHERE app_id = ? AND id = ? AND is_current = 1
                """,
                (_now(), app_id, provider_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def delete_agent_provider(self, app_id: str, provider_id: str) -> bool:
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM agent_providers WHERE app_id = ? AND id = ?",
                (app_id, provider_id),
            )
            self._connection.commit()
            return cursor.rowcount > 0

    def reorder_agent_resources(
        self,
        resource_type: str,
        resource_ids: list[str],
        *,
        client_id: str | None = None,
    ) -> list[str]:
        tables = {
            "mcp": "agent_mcp_servers",
            "skill": "agent_skills",
            "prompt": "agent_prompts",
            "profile": "agent_profiles",
            "provider": "agent_providers",
        }
        table = tables.get(resource_type)
        if table is None:
            raise ValueError(f"不支持的 resource_type: {resource_type}")

        ordered_ids = [str(resource_id) for resource_id in resource_ids]
        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError("resource_ids 包含重复 ID")
        if resource_type == "provider" and not client_id:
            raise ValueError("Provider 排序必须提供 client_id")

        where = " WHERE app_id = ?" if resource_type == "provider" else ""
        params = (client_id,) if resource_type == "provider" else ()
        with self._lock:
            rows = self._connection.execute(
                f"SELECT id FROM {table}{where}", params
            ).fetchall()
            existing_ids = {str(row["id"]) for row in rows}
            if len(ordered_ids) != len(existing_ids) or set(ordered_ids) != existing_ids:
                raise ValueError("resource_ids 必须包含该分类的完整资源集合")

            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if resource_type == "provider":
                    self._connection.executemany(
                        "UPDATE agent_providers SET sort_index = ? WHERE app_id = ? AND id = ?",
                        [
                            (index, client_id, resource_id)
                            for index, resource_id in enumerate(ordered_ids)
                        ],
                    )
                else:
                    self._connection.executemany(
                        f"UPDATE {table} SET sort_index = ? WHERE id = ?",
                        [
                            (index, resource_id)
                            for index, resource_id in enumerate(ordered_ids)
                        ],
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return ordered_ids

    def _next_sort_index(self, table: str, *, app_id: str | None = None) -> int:
        allowed_tables = {
            "agent_mcp_servers",
            "agent_skills",
            "agent_prompts",
            "agent_profiles",
            "agent_providers",
        }
        if table not in allowed_tables:
            raise ValueError(f"不支持排序的表: {table}")
        if table == "agent_providers":
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sort_index), -1) + 1 AS next_index "
                "FROM agent_providers WHERE app_id = ?",
                (app_id,),
            ).fetchone()
        else:
            row = self._connection.execute(
                f"SELECT COALESCE(MAX(sort_index), -1) + 1 AS next_index FROM {table}"
            ).fetchone()
        return int(row["next_index"] if row is not None else 0)

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
                    sort_index INTEGER NOT NULL DEFAULT 0,
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
                    description TEXT,
                    content TEXT NOT NULL,
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_providers (
                    id TEXT NOT NULL,
                    app_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    meta_json TEXT NOT NULL DEFAULT '{}',
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

                CREATE TABLE IF NOT EXISTS agent_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    source TEXT,
                    source_kind TEXT NOT NULL,
                    ssot_path TEXT NOT NULL,
                    version TEXT,
                    content_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_skill_variants (
                    skill_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    install_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (skill_id, client_id, platform),
                    FOREIGN KEY (skill_id) REFERENCES agent_skills(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_skill_assignments (
                    node_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    variant_client_id TEXT NOT NULL,
                    variant_platform TEXT NOT NULL,
                    desired_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_id, client_id, skill_id),
                    FOREIGN KEY (skill_id) REFERENCES agent_skills(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_skill_observations (
                    node_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    skill_id TEXT NOT NULL,
                    present INTEGER NOT NULL,
                    content_hash TEXT,
                    status TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (node_id, client_id, skill_id)
                );

                CREATE TABLE IF NOT EXISTS agent_prompt_variants (
                    prompt_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (prompt_id, client_id, platform),
                    FOREIGN KEY (prompt_id) REFERENCES agent_prompts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_prompt_assignments (
                    node_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    prompt_id TEXT NOT NULL,
                    variant_client_id TEXT NOT NULL,
                    variant_platform TEXT NOT NULL,
                    desired_enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_id, client_id),
                    FOREIGN KEY (prompt_id) REFERENCES agent_prompts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_prompt_observations (
                    node_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    prompt_id TEXT NOT NULL,
                    present INTEGER NOT NULL,
                    content_hash TEXT,
                    status TEXT NOT NULL,
                    scanned_at TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (node_id, client_id, prompt_id)
                );

                CREATE TABLE IF NOT EXISTS agent_router_nodes (
                    node_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    listen_address TEXT NOT NULL DEFAULT '127.0.0.1',
                    listen_port INTEGER NOT NULL DEFAULT 7888,
                    show_home_switch INTEGER NOT NULL DEFAULT 1,
                    outbound_proxy TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_router_clients (
                    node_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    provider_id TEXT,
                    provider_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    takeover_enabled INTEGER NOT NULL DEFAULT 0,
                    auto_failover INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 0,
                    failure_threshold INTEGER NOT NULL DEFAULT 3,
                    cooldown_seconds INTEGER NOT NULL DEFAULT 60,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_id, client_id),
                    FOREIGN KEY (node_id) REFERENCES agent_router_nodes(node_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_route_failover_queue (
                    node_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    sort_index INTEGER NOT NULL,
                    PRIMARY KEY (node_id, client_id, provider_id),
                    FOREIGN KEY (node_id, client_id)
                        REFERENCES agent_router_clients(node_id, client_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS agent_profiles (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT,
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_profile_items (
                    profile_id TEXT NOT NULL,
                    client_id TEXT NOT NULL,
                    resource_type TEXT NOT NULL CHECK (resource_type IN ('provider', 'mcp', 'skill', 'prompt', 'router')),
                    resource_id TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    sort_index INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (profile_id, client_id, resource_type, resource_id),
                    FOREIGN KEY (profile_id) REFERENCES agent_profiles(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_agent_skill_assignments_target
                ON agent_skill_assignments(node_id, client_id, desired_enabled);

                CREATE INDEX IF NOT EXISTS idx_agent_prompt_assignments_target
                ON agent_prompt_assignments(node_id, client_id, desired_enabled);

                CREATE INDEX IF NOT EXISTS idx_agent_profile_items_profile
                ON agent_profile_items(profile_id, client_id, resource_type, sort_index);

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
            for table in (
                "agent_mcp_servers",
                "agent_skills",
                "agent_prompts",
                "agent_profiles",
                "agent_providers",
            ):
                self._add_column_if_missing(
                    table,
                    "sort_index",
                    f"ALTER TABLE {table} ADD COLUMN sort_index INTEGER NOT NULL DEFAULT 0",
                )
            self._add_column_if_missing(
                "agent_prompts",
                "description",
                "ALTER TABLE agent_prompts ADD COLUMN description TEXT",
            )
            self._add_column_if_missing(
                "agent_providers",
                "meta_json",
                "ALTER TABLE agent_providers ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'",
            )
            try:
                self._migrate_agent_provider_local_current()
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _migrate_agent_provider_local_current(self) -> None:
        legacy_rows = self._connection.execute(
            """
            SELECT app_id, settings_json, meta_json
            FROM agent_providers AS legacy
            WHERE id = 'local-current'
              AND NOT EXISTS (
                  SELECT 1
                  FROM agent_providers AS target
                  WHERE target.app_id = legacy.app_id AND target.id = 'default'
              )
            ORDER BY app_id
            """
        ).fetchall()
        for row in legacy_rows:
            app_id = str(row["app_id"])
            old_ref = f"provider-secret:{app_id}:local-current"
            new_ref = f"provider-secret:{app_id}:default"
            settings = _replace_json_string(
                json.loads(row["settings_json"] or "{}"), old_ref, new_ref
            )
            meta = _replace_json_string(
                json.loads(row["meta_json"] or "{}"), old_ref, new_ref
            )
            self._connection.execute(
                """
                UPDATE agent_providers
                SET id = 'default', settings_json = ?, meta_json = ?
                WHERE app_id = ? AND id = 'local-current'
                """,
                (
                    json.dumps(settings, ensure_ascii=False, sort_keys=True),
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                    app_id,
                ),
            )
            self._connection.execute(
                """
                UPDATE agent_router_clients
                SET provider_id = 'default'
                WHERE client_id = ? AND provider_id = 'local-current'
                """,
                (app_id,),
            )
            duplicate_queues = self._connection.execute(
                """
                SELECT legacy.node_id, legacy.sort_index AS legacy_sort,
                       target.sort_index AS target_sort
                FROM agent_route_failover_queue AS legacy
                JOIN agent_route_failover_queue AS target
                  ON target.node_id = legacy.node_id
                 AND target.client_id = legacy.client_id
                 AND target.provider_id = 'default'
                WHERE legacy.client_id = ?
                  AND legacy.provider_id = 'local-current'
                """,
                (app_id,),
            ).fetchall()
            for queue in duplicate_queues:
                self._connection.execute(
                    """
                    UPDATE agent_route_failover_queue
                    SET sort_index = ?
                    WHERE node_id = ? AND client_id = ? AND provider_id = 'default'
                    """,
                    (
                        min(int(queue["legacy_sort"]), int(queue["target_sort"])),
                        queue["node_id"],
                        app_id,
                    ),
                )
                self._connection.execute(
                    """
                    DELETE FROM agent_route_failover_queue
                    WHERE node_id = ? AND client_id = ? AND provider_id = 'local-current'
                    """,
                    (queue["node_id"], app_id),
                )
            self._connection.execute(
                """
                UPDATE agent_route_failover_queue
                SET provider_id = 'default'
                WHERE client_id = ? AND provider_id = 'local-current'
                """,
                (app_id,),
            )
            self._connection.execute(
                """
                DELETE FROM agent_profile_items
                WHERE client_id = ?
                  AND resource_type = 'provider'
                  AND resource_id = 'default'
                  AND EXISTS (
                      SELECT 1
                      FROM agent_profile_items AS legacy
                      WHERE legacy.profile_id = agent_profile_items.profile_id
                        AND legacy.client_id = agent_profile_items.client_id
                        AND legacy.resource_type = 'provider'
                        AND legacy.resource_id = 'local-current'
                  )
                """,
                (app_id,),
            )
            self._connection.execute(
                """
                UPDATE agent_profile_items
                SET resource_id = 'default'
                WHERE client_id = ?
                  AND resource_type = 'provider'
                  AND resource_id = 'local-current'
                """,
                (app_id,),
            )
        if legacy_rows:
            pending_row = self._connection.execute(
                "SELECT value_json FROM agent_settings WHERE key = ?",
                ("agent_provider_local_current_secret_migration_v1",),
            ).fetchone()
            try:
                pending = json.loads(pending_row["value_json"]) if pending_row else []
            except (TypeError, json.JSONDecodeError):
                pending = []
            pending_apps = {str(value) for value in pending if str(value)}
            pending_apps.update(str(row["app_id"]) for row in legacy_rows)
            self._connection.execute(
                """
                INSERT INTO agent_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (
                    "agent_provider_local_current_secret_migration_v1",
                    json.dumps(sorted(pending_apps), ensure_ascii=False),
                    _now(),
                ),
            )

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
        "meta": json.loads(row["meta_json"] or "{}"),
        "website_url": row["website_url"],
        "category": row["category"],
        "notes": row["notes"],
        "icon": row["icon"],
        "icon_color": row["icon_color"],
        "is_current": bool(row["is_current"]),
        "sort_index": int(row["sort_index"]),
        "source": row["source"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _replace_json_string(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {
            key: _replace_json_string(child, old, new)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_replace_json_string(child, old, new) for child in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
