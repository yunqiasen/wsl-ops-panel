"""Queue order and exact cleanup remain independent of the business lifecycle."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
    wait_for_task,
)


def test_close_survives_removal_of_the_project_containers(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        panel.containers.clear()
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "off"
        assert not any(value["active"] for value in runtime.resources.values())
    finally:
        panel.app.state.task_worker.stop()


def test_concurrent_open_requests_share_one_resource_and_queued_close_wins(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    started, release = Event(), Event()

    class DelayedRuntime(FakeTunnelRuntime):
        def start(self, *args, **kwargs):
            value = super().start(*args, **kwargs)
            started.set()
            return value

        def observe(self, handle, **kwargs):
            value = super().observe(handle, **kwargs)
            if not release.is_set():
                value["url"] = None
            return value

    runtime = DelayedRuntime()
    panel.app.state.cf_service.runtime = runtime
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(
            executor.map(
                lambda _: panel.client.post(
                    "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
                ).json(),
                range(2),
            )
        )
    assert all(item["queued_count"] == 1 for item in responses)
    panel.app.state.task_worker.start()
    try:
        assert started.wait(2)
        closing = panel.client.post(
            "/api/bulk/actions/cf-disable", json={"asset_ids": ["web"]}
        ).json()
        assert closing["queued_count"] == 1
        status = panel.client.get("/api/assets/web/cf").json()
        assert status["state"] == "stopping"
        assert status["current_url"] is None
        release.set()
        assert wait_for_task(panel, closing["tasks"][0]["id"]).status == "succeeded"
        assert len(runtime.resources) == 1
        resource = next(iter(runtime.resources.values()))
        resource["url"] = "https://late-fixture.trycloudflare.com"
        assert resource["active"] is False
        assert panel.client.get("/api/assets/web/cf").json()["state"] == "off"
        assert panel.client.get("/api/assets/web/cf").json()["current_url"] is None
        assert panel.containers[0].state == "running"
    finally:
        release.set()
        panel.app.state.task_worker.stop()
