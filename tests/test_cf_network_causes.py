"""Network layers come from causes, not words in endpoint names."""

import socket
import ssl

import httpcore
import httpx
import pytest

from tests.test_cf_tunnels import FakeTunnelRuntime, attach_local_observation, make_panel, submit_cf


@pytest.mark.parametrize("case,label", [("url_words", "TCP"), ("dns_cause", "DNS"), ("tls_cause", "TLS")])
def test_cf_network_classification_uses_transport_causes_not_url_substrings(
    tmp_path, monkeypatch, case, label
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    service = panel.app.state.cf_service
    service.runtime = FakeTunnelRuntime()
    service.public_timeout = 0.01

    def respond(request):
        if not request.url.host.endswith("trycloudflare.com"):
            return httpx.Response(200, text="business fixture")
        if case == "url_words":
            raise httpx.ConnectError("connection failed for https://dns-fixture.trycloudflare.com/ssl/private", request=request)
        error = socket.gaierror(-2, "private cause") if case == "dns_cause" else ssl.SSLCertVerificationError(1, "private cause")
        transport = httpcore.ConnectError("transport failure")
        transport.__cause__ = error
        raise httpx.ConnectError("public read failure", request=request) from transport

    service.http_transport = httpx.MockTransport(respond)
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "failed"
        status = panel.client.get("/api/assets/web/cf").json()
        assert label in status["error"]["message"]
        assert "private" not in str(status)
        assert status["current_url"] is None
    finally:
        panel.app.state.task_worker.stop()
