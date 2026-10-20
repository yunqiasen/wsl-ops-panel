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
