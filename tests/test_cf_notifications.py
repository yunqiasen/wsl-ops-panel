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


def test_sender_reloads_cf_state_instead_of_reusing_queued_asset_json(
    tmp_path, monkeypatch
):
    import json
    import yaml
    from app.models.assets import AssetSnapshot
    from app.services.notifications import (
        NotificationService,
        asset_to_notification_json,
    )

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    panel.app.state.cf_service.runtime = FakeTunnelRuntime()
    config = tmp_path / "config/notifications/projects.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        yaml.safe_dump(
            {
                "pushplus": {"token": "notification-fixture-token"},
                "assets": {
                    "web": {
                        "enabled": True,
                        "title": "Fixture",
                        "template": "CF={{ cf_url }}",
                    }
                },
            }
        )
    )
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        snapshot_json = asset_to_notification_json(
            panel.app.state.asset_service.get_asset("web")
        )
        assert "https://business-fixture.trycloudflare.com/" in snapshot_json
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        observed.requests.clear()
        # Same public sender entry and serialized snapshot used by the task CLI.
        sender = NotificationService(tmp_path / "config")
        payloads = []
        sender.send(
            AssetSnapshot.model_validate_json(snapshot_json),
            sender=lambda payload: payloads.append(json.loads(payload)),
        )
        assert len(payloads) == 1
        assert payloads[0]["content"] == "CF="
        assert observed.requests == []
    finally:
        panel.app.state.task_worker.stop()
