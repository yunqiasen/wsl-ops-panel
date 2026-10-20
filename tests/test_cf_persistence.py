"""Faults in the two persistent stores never turn an uncommitted action into success."""

import sqlite3

from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
)


def test_failed_task_insert_is_settled_with_a_sanitized_persistence_error(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime

    def failed_insert(task):
        raise sqlite3.OperationalError("disk full; private-value=fixture-secret")

    monkeypatch.setattr(panel.store, "insert", failed_insert)
    result = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    ).json()
    assert result["queued_count"] == 0
    status = panel.client.get("/api/assets/web/cf").json()
    assert status["state"] == "error"
    assert status["error"]["stage"] == "persistence"
    assert status["pending_action"] is None
    assert status["current_url"] is None
    assert "fixture-secret" not in str(result) + str(status)
    assert runtime.resources == {}
    assert panel.store.list_all() == []
