"""Public and local HTTP pages must be application responses, not CF interstitials."""

import httpx
import pytest

from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


@pytest.mark.parametrize(
    "status,headers,body",
    [
        (403, {"cf-mitigated": "challenge"}, "private challenge fixture"),
        (
            200,
            {},
            "<title>Just a moment...</title><script>window._cf_chl_opt={}</script>",
        ),
        (403, {}, '<html><div id="cf-error-details">Cloudflare error</div></html>'),
        (200, {}, "<title>Cloudflare Tunnel error</title><h1>Error 1033</h1>"),
    ],
)
def test_cf_challenges_and_error_pages_are_not_application_login_entries(
    tmp_path, monkeypatch, status, headers, body
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    service = panel.app.state.cf_service
    service.runtime = runtime
    service.http_transport = httpx.MockTransport(
        lambda request: httpx.Response(status, headers=headers, text=body)
    )
    prepared = panel.client.get("/api/assets/web/cf/targets").json()
    assert not any(target["reachable"] for target in prepared["targets"])
    assert not any(target["requires_login"] for target in prepared["targets"])
    result = panel.client.post(
        "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
    ).json()
    assert result["queued_count"] == 0
    assert runtime.resources == {}
    assert panel.client.get("/api/assets/web/cf").json()["current_url"] is None
    assert "private challenge fixture" not in str(prepared) + str(result)


@pytest.mark.parametrize("status", [401, 403])
def test_matching_application_login_response_remains_an_eligible_entry(
    tmp_path, monkeypatch, status
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    service = panel.app.state.cf_service
    service.runtime = FakeTunnelRuntime()
    service.http_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status,
            text="Business fixture needs login",
            headers={"server": "cloudflare", "content-type": "text/html"},
        )
    )
    prepared = panel.client.get("/api/assets/web/cf/targets").json()
    assert prepared["targets"][0]["requires_login"] is True
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "online"
    finally:
        panel.app.state.task_worker.stop()
