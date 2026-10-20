"""Browser drives the real HTTP -> queue -> lifecycle path; only OS/network are fixtures."""

import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
from time import monotonic, sleep

import uvicorn
import httpx
import pytest

from app.core.security import COOKIE_NAME, issue_session_token
from tests.test_cf_legacy_routes import attach_legacy
from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
)


@pytest.mark.parametrize("scenario", ["single", "multiple", "legacy", "hint", "login"])
def test_browser_cf_on_off(tmp_path, monkeypatch, scenario):
    panel = make_panel(tmp_path)
    observed = attach_local_observation(panel, monkeypatch)
    multiple = scenario == "multiple"
    if multiple:
        observed.info["ports"]["9090/tcp"] = [
            {"HostIp": "127.0.0.1", "HostPort": "19090"}
        ]
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
    if scenario == "login":
        panel.app.state.cf_service.http_transport = httpx.MockTransport(
            lambda request: httpx.Response(403, text="Business fixture needs login")
        )
    legacy = None
    if scenario == "legacy":
        legacy = attach_legacy(panel, tmp_path, monkeypatch)
        runtime = panel.app.state.cf_service.runtime
    elif scenario == "hint":
        script = panel.project / "scripts/cftunnel-start.sh"
        script.parent.mkdir()
        script.write_text("#!/bin/sh\necho fixture\n")
        panel.app.state.cf_service.legacy_unit_roots = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(panel.app, log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = monotonic() + 5
    while not server.started and monotonic() < deadline:
        sleep(0.01)
    assert server.started
    try:
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("cf-tunnels.cjs"))],
            env={
                **os.environ,
                "CF_BROWSER_URL": f"http://127.0.0.1:{port}",
                "CF_BROWSER_MULTIPLE": str(int(multiple)),
                "CF_BROWSER_SCENARIO": scenario,
                "CF_BROWSER_EXPECTED_URL": "https://legacy-fixture.trycloudflare.com/" if legacy else "https://business-fixture.trycloudflare.com/",
                "CF_COOKIE_NAME": COOKIE_NAME,
                "CF_COOKIE_TOKEN": issue_session_token(),
            },
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
        if scenario == "hint":
            assert not panel.store.list_all()
            assert not observed.requests
            assert runtime.resources == {}
            return
        assert [task.status for task in panel.store.list_all()] == ["succeeded"] * 3
        if legacy:
            assert legacy.info["ActiveState"] == "active"
            assert runtime.resources == {}
            assert panel.containers[0].state == "running"
            return
        assert len(runtime.resources) == 2
        if multiple:
            assert {value["origin"] for value in runtime.resources.values()} == {
                "http://127.0.0.1:19090"
            }
        assert sum(resource["active"] for resource in runtime.resources.values()) == 1
        assert panel.containers[0].state == "running"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
