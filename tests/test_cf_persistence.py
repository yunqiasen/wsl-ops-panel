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


def test_status_read_during_cross_store_enqueue_does_not_cancel_the_request(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from tests.test_cf_tunnels import wait_for_task

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    panel.app.state.cf_service.runtime = FakeTunnelRuntime()
    entered, release = Event(), Event()
    original_insert = panel.store.insert

    def delayed_insert(task):
        entered.set()
        assert release.wait(3)
        original_insert(task)

    monkeypatch.setattr(panel.store, "insert", delayed_insert)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(
            panel.client.post,
            "/api/bulk/actions/cf-create",
            json={"asset_ids": ["web"]},
        )
        try:
            assert entered.wait(3)
            status = panel.client.get("/api/assets/web/cf").json()
            assert status["state"] == "starting"
            assert status["current_url"] is None
        finally:
            release.set()
        response = pending.result(timeout=3).json()
    panel.app.state.task_worker.start()
    try:
        assert wait_for_task(panel, response["tasks"][0]["id"]).status == "succeeded"
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "online"
    finally:
        panel.app.state.task_worker.stop()
