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


def test_explicit_choice_is_checked_then_re_resolved_on_reopen(tmp_path, monkeypatch):
    from tests.test_cf_tunnels import wait_for_task

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    observed.info["ports"]["9090/tcp"] = [{"HostIp": "127.0.0.1", "HostPort": "19090"}]
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    targets = panel.client.get("/api/assets/web/cf/targets").json()["targets"]
    assert len(targets) == 2
    selected = next(target for target in targets if target["origin"].endswith(":19090"))
    choice = {key: selected[key] for key in ("id", "revision")}
    response = panel.client.post(
        "/api/bulk/actions/cf-create",
        json={"asset_ids": ["web"], "cf_target_map": {"web": choice}},
    )
    assert response.status_code == 202
    assert response.json()["queued_count"] == 1
    panel.app.state.task_worker.start()
    try:
        assert (
            wait_for_task(panel, response.json()["tasks"][0]["id"]).status
            == "succeeded"
        )
        assert (
            next(iter(runtime.resources.values()))["origin"] == "http://127.0.0.1:19090"
        )
        assert submit_cf(panel, "cf-disable").status == "succeeded"
        observed.info["ports"]["9090/tcp"][0]["HostPort"] = "19091"
        observed.info["id"] = "b" * 64
        panel.containers[0].id = "b" * 64
        stale = panel.client.post(
            "/api/bulk/actions/cf-create",
            json={"asset_ids": ["web"], "cf_target_map": {"web": choice}},
        )
        assert stale.json()["queued_count"] == 0
        assert not any(resource["active"] for resource in runtime.resources.values())
        assert (
            panel.client.get("/api/assets/web/cf/targets").json()["selected_id"]
            == choice["id"]
        )
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert [
            resource["origin"]
            for resource in runtime.resources.values()
            if resource["active"]
        ] == ["http://127.0.0.1:19091"]
    finally:
        panel.app.state.task_worker.stop()


def test_equivalent_container_network_addresses_choose_a_reachable_route(
    tmp_path, monkeypatch
):
    import httpx

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    observed.info["ports"] = {"8080/tcp": None}
    observed.info["networks"] = {
        "isolated": {"IPAddress": "172.21.0.2"},
        "reachable": {"IPAddress": "172.22.0.2"},
    }

    def respond(request):
        if request.url.host == "172.21.0.2":
            raise httpx.ConnectError("route unavailable", request=request)
        return httpx.Response(200, text="business fixture")

    panel.app.state.cf_service.http_transport = httpx.MockTransport(respond)
    response = panel.client.get("/api/assets/web/cf/targets")
    assert response.status_code == 200, response.json()
    assert len(response.json()["targets"]) == 1
    target = response.json()["targets"][0]
    assert target["origin"] == "http://172.22.0.2:8080"
    assert target["reachable"] is True
    assert panel.store.list_all() == []


def test_all_owned_compose_services_are_candidates_not_only_primary(
    tmp_path, monkeypatch
):
    import json
    import subprocess

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    admin = panel.containers[0].model_copy(
        update={
            "id": "b" * 64,
            "name": "business-admin",
            "compose_service": "admin",
            "labels": {
                **panel.containers[0].labels,
                "com.docker.compose.service": "admin",
            },
        }
    )
    panel.containers.append(admin)
    original_run = subprocess.run

    def run(command, **kwargs):
        if (
            command[:3] == ["docker", "container", "inspect"]
            and command[-1] == "business-admin"
        ):
            info = {
                **observed.info,
                "id": admin.id,
                "name": "/business-admin",
                "labels": admin.labels,
                "ports": {"9090/tcp": [{"HostIp": "127.0.0.1", "HostPort": "19090"}]},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(info), "")
        return original_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    response = panel.client.get("/api/assets/web/cf/targets")
    assert response.status_code == 200
    assert {target["name"] for target in response.json()["targets"]} == {
        "web · 8080/tcp",
        "admin · 9090/tcp",
    }
    assert response.json()["requires_selection"] is True
    assert (
        panel.client.post(
            "/api/bulk/actions/cf-create", json={"asset_ids": ["web"]}
        ).json()["queued_count"]
        == 0
    )
    assert panel.store.list_all() == []
