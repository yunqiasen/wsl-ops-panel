"""Current observations, expiry and recovery through the panel's normal CF routes."""

from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


def test_exited_tunnel_is_not_reported_online_from_a_saved_url(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        resource = next(iter(runtime.resources.values()))
        resource["active"] = False
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["current_url"] is None
        assert status["history_url"] == "https://business-fixture.trycloudflare.com/"
        assert status["error"]["stage"] == "runtime"
        assert panel.containers[0].state == "running"
    finally:
        panel.app.state.task_worker.stop()


def test_public_verification_expires_in_sixty_seconds_and_boot_change_clears_switch(
    tmp_path, monkeypatch
):
    from time import time

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    service = panel.app.state.cf_service
    service.runtime = FakeTunnelRuntime()
    clock = [time()]
    service.clock = lambda: clock[0]
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        clock[0] += 61
        expired = panel.client.get("/api/assets/web/cf").json()
        assert expired["state"] == "error"
        assert expired["current_url"] is None
        assert expired["error"]["stage"] == "freshness"
        service.runtime.boot_id = "different-boot"
        rebooted = panel.client.get("/api/assets/web/cf").json()
        assert rebooted["state"] == "off"
        assert rebooted["desired"] == "off"
        assert rebooted["current_url"] is None
        assert rebooted["history_url"]
    finally:
        panel.app.state.task_worker.stop()


def test_maintenance_rechecks_online_entries_and_cleans_only_a_stopped_source_tunnel(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    service = panel.app.state.cf_service
    runtime = FakeTunnelRuntime()
    service.runtime = runtime
    clock = [1792460000.1234567]
    service.clock = lambda: clock[0]
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        observed.requests.clear()
        clock[0] += 29
        service.maintain()
        assert not any("trycloudflare.com" in url for url in observed.requests)
        clock[0] += 2
        service.maintain()
        assert sum("trycloudflare.com" in url for url in observed.requests) == 1
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "online", status
        observed.info["running"] = False
        service.maintain()
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "error"
        assert status["error"]["stage"] == "origin_probe"
        assert status["current_url"] is None
        assert not any(value["active"] for value in runtime.resources.values())
        assert (
            panel.containers[0].state == "running"
        )  # CF never issued a business stop command.
    finally:
        panel.app.state.task_worker.stop()
