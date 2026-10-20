"""Runtime-derived candidates and selections through authenticated project endpoints."""

from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
    submit_cf,
)


def test_unpublished_docker_port_uses_container_network_not_same_number_on_host(
    tmp_path, monkeypatch
):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    observed.info["ports"] = {"8080/tcp": None}
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    prepared = panel.client.get("/api/assets/web/cf/targets")
    assert prepared.status_code == 200
    assert len(prepared.json()["targets"]) == 1
    target = prepared.json()["targets"][0]
    assert target["origin"] == "http://172.20.0.2:8080"
    assert target["source"] == "container_network"
    assert observed.requests == ["http://172.20.0.2:8080/"]
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert (
            next(iter(runtime.resources.values()))["origin"] == "http://172.20.0.2:8080"
        )
        assert observed.info["ports"] == {"8080/tcp": None}
    finally:
        panel.app.state.task_worker.stop()
