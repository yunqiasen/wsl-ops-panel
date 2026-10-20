"""Notification previews consume the same current CF observation as the panel."""

from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


def test_closing_cf_does_not_restore_a_historical_domain_in_notification_preview(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    panel.app.state.cf_service.runtime = FakeTunnelRuntime()
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        saved = panel.client.post(
            "/api/notifications/web",
            json={
                "enabled": True,
                "title": "Fixture",
                "template": "CF={{ cf_url }}",
            },
        ).json()
        assert (
            saved["preview"]["content"]
            == "CF=https://business-fixture.trycloudflare.com/"
        )
        logs = panel.project / "logs"
        logs.mkdir()
        history = logs / "cftunnel-domain.txt"
        history.write_text("https://historical-fixture.trycloudflare.com\n")
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        preview = panel.client.get("/api/notifications/web").json()["preview"]
        assert preview["content"] == "CF="
        assert history.read_text() == "https://historical-fixture.trycloudflare.com\n"
    finally:
        panel.app.state.task_worker.stop()
