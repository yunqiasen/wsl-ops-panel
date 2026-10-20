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


def test_project_uses_owned_listener_not_a_port_hint_and_rechecks_pid(
    tmp_path, monkeypatch
):
    import subprocess
    from app.models.assets import AssetSnapshot
    from tests.test_cf_tunnels import wait_for_task

    root = tmp_path / "local-project"
    root.mkdir()
    (root / "app.py").write_text("# a source project, not a launch command\n")
    project = AssetSnapshot(
        object_id="project__fixture",
        name="Local Project",
        category="project",
        status="present",
        metadata={"path": str(root), "web_ui": {"port": "19999", "url_path": "/"}},
    )
    panel = make_panel(tmp_path, project_scanner=lambda: [project])
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    service = panel.app.state.cf_service
    service.runtime = runtime
    service.proc_root = tmp_path / "proc"
    pid = service.proc_root / "3101"
    pid.mkdir(parents=True)
    (pid / "stat").write_text("3101 (python) " + " ".join(["S"] + ["0"] * 18 + ["101"]))
    (pid / "comm").write_text("python\n")
    (pid / "cgroup").write_text("0::/user.slice/user-1000.slice/session.scope\n")
    (pid / "cwd").symlink_to(root, target_is_directory=True)
    current = {"pid": 3101}
    original = subprocess.run

    def run(command, **kwargs):
        if command[0] == "ss":
            text = f'LISTEN 0 128 127.0.0.1:18181 0.0.0.0:* users:(("python",pid={current["pid"]},fd=3))\n'
            return subprocess.CompletedProcess(command, 0, text, "")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    prepared = panel.client.get("/api/assets/project__fixture/cf/targets")
    assert prepared.status_code == 200, prepared.json()
    target = prepared.json()["targets"][0]
    assert target["origin"] == "http://127.0.0.1:18181"
    assert target["source"] == "project_listener"
    panel.app.state.task_worker.start()
    try:

        def submit(action, choice=None):
            payload = {"asset_ids": [project.object_id]}
            if choice:
                payload["cf_target_map"] = {project.object_id: choice}
            return panel.client.post(f"/api/bulk/actions/{action}", json=payload).json()

        started = submit("cf-create")
        assert started["queued_count"] == 1, started
        assert wait_for_task(panel, started["tasks"][0]["id"]).status == "succeeded"
        stopped = submit("cf-disable")
        assert wait_for_task(panel, stopped["tasks"][0]["id"]).status == "succeeded"
        pid.rename(service.proc_root / "3102")
        current["pid"] = 3102
        old = submit("cf-create", {k: target[k] for k in ("id", "revision")})
        assert old["queued_count"] == 0
        reopened = submit("cf-create")
        assert reopened["queued_count"] == 1
        assert wait_for_task(panel, reopened["tasks"][0]["id"]).status == "succeeded"
        assert len(runtime.resources) == 2
        assert (
            root / "app.py"
        ).read_text() == "# a source project, not a launch command\n"
        assert current["pid"] == 3102
    finally:
        panel.app.state.task_worker.stop()


def test_host_network_uses_only_listeners_of_current_container_processes(
    tmp_path, monkeypatch
):
    import subprocess

    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    observed.info.update(network_mode="host", ports={}, pid=4201)
    service = panel.app.state.cf_service
    service.proc_root = tmp_path / "proc"
    for number, group in [
        (4201, "docker-" + "a" * 64 + ".scope"),
        (4202, "session.scope"),
    ]:
        pid = service.proc_root / str(number)
        pid.mkdir(parents=True)
        (pid / "stat").write_text(
            f"{number} (python) " + " ".join(["S"] + ["0"] * 18 + ["101"])
        )
        (pid / "comm").write_text("python\n")
        (pid / "cgroup").write_text(f"0::/{group}\n")
        (pid / "cwd").symlink_to(panel.project)
    original = subprocess.run

    def run(command, **kwargs):
        if command[0] == "ss":
            text = "\n".join(
                [
                    'LISTEN 0 128 [::]:18282 [::]:* users:(("python",pid=4201,fd=3))',
                    'LISTEN 0 128 127.0.0.1:8080 0.0.0.0:* users:(("python",pid=4202,fd=3))',
                ]
            )
            return subprocess.CompletedProcess(command, 0, text, "")
        if command[:2] == ["docker", "top"]:
            assert command[2] == "a" * 64
            return subprocess.CompletedProcess(command, 0, "PID\n4201\n", "")
        return original(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    response = panel.client.get("/api/assets/web/cf/targets")
    assert response.status_code == 200, response.json()
    targets = response.json()["targets"]
    assert len(targets) == 1
    assert targets[0]["source"] == "host_network"
    assert targets[0]["origin"] == "http://[::1]:18282"
    assert observed.requests == ["http://[::1]:18282/"]
    assert not panel.store.list_all()


def test_existing_page_path_and_same_origin_login_redirect_are_verified(
    tmp_path, monkeypatch
):
    import httpx
    import yaml

    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    config = tmp_path / "config/objects/web.yaml"
    settings = yaml.safe_load(config.read_text())
    settings["config"]["endpoints"] = [{"container_port": "8080", "path": "/dashboard"}]
    config.write_text(yaml.safe_dump(settings))
    panel.app.state.rebuild_registry_runtime()
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/dashboard":
            return httpx.Response(302, headers={"location": "/login"})
        if request.url.path == "/login":
            return httpx.Response(
                401,
                text="Business fixture login",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404, text="not here")

    panel.app.state.cf_service.http_transport = httpx.MockTransport(respond)
    prepared = panel.client.get("/api/assets/web/cf/targets")
    assert prepared.status_code == 200
    target = prepared.json()["targets"][0]
    assert target["reachable"] is True
    assert target["path"] == "/dashboard"
    assert target["protocol"] == "http"
    assert target["requires_login"] is True
    panel.app.state.task_worker.start()
    try:
        assert submit_cf(panel, "cf-create").status == "succeeded"
        assert (
            panel.client.get("/api/assets/web/cf").json()["current_url"]
            == "https://business-fixture.trycloudflare.com/dashboard"
        )
        assert "https://business-fixture.trycloudflare.com/login" in requests
    finally:
        panel.app.state.task_worker.stop()
