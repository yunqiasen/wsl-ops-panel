"""The CF card distinguishes login-gated access from withdrawn historical URLs."""

import httpx
import pytest

from tests.test_cf_tunnels import FakeTunnelRuntime, attach_local_observation, make_panel, submit_cf


@pytest.mark.parametrize("http_status", [401, 403])
def test_cf_card_explains_that_verified_application_access_requires_login(
    tmp_path, monkeypatch, http_status
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    service = panel.app.state.cf_service
    service.runtime = FakeTunnelRuntime()
    service.http_transport = httpx.MockTransport(
        lambda request: httpx.Response(http_status, text="Business login fixture")
    )
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "online"
        page = panel.client.get("/categories/docker")
        assert "可达，需要登录" in page.text
    finally:
        panel.app.state.task_worker.stop()

