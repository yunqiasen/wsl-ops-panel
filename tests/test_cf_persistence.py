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


def test_sqlite_publication_failure_cleans_the_uncommitted_resource(
    tmp_path, monkeypatch
):
    from tests.test_cf_tunnels import submit_cf

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    service = panel.app.state.cf_service
    runtime = FakeTunnelRuntime()
    service.runtime = runtime
    # A real SQLite constraint fault at the publication boundary, not fake CF rows.
    with sqlite3.connect(service.store.path) as db:
        db.execute(
            "CREATE TABLE cf_tunnels (owner TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        db.execute("""
            CREATE TRIGGER fail_online_publication BEFORE UPDATE ON cf_tunnels
            WHEN json_extract(NEW.payload, '$.state') = 'online'
            BEGIN SELECT RAISE(FAIL, 'private fixture credential'); END
        """)
    panel.app.state.task_worker.start()
    try:
        task = submit_cf(panel, "cf-create")
        assert task.status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "persistence"
        assert status["pending_action"] is None
        assert status["current_url"] is None
        assert "private fixture credential" not in str(status)
        assert not any(item["active"] for item in runtime.resources.values())
        assert submit_cf(panel, "cf-disable").status == "succeeded"
    finally:
        panel.app.state.task_worker.stop()


def test_task_result_write_failure_does_not_strand_a_running_task_or_block_queue(
    tmp_path, monkeypatch
):
    import sys
    from app.adapters.base import ActionPlan
    from tests.test_cf_tunnels import wait_for_task

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    original_update = panel.store.update

    def failing_result(task):
        if task.action == "cf_create" and task.status == "succeeded":
            raise sqlite3.OperationalError("result-write fixture")
        original_update(task)

    monkeypatch.setattr(panel.store, "update", failing_result)
    result = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    ).json()
    ordinary = panel.app.state.task_queue.enqueue(
        "ordinary",
        "start",
        plan=ActionPlan(commands=[[sys.executable, "-c", "print('queue is free')"]]),
    )
    panel.app.state.task_worker.start()
    try:
        assert wait_for_task(panel, ordinary.id).status == "succeeded"
        task = panel.store.get(result["tasks"][0]["id"])
        assert task.status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "persistence"
        assert status["current_url"] is None
        assert not any(item["active"] for item in runtime.resources.values())
    finally:
        panel.app.state.task_worker.stop()
