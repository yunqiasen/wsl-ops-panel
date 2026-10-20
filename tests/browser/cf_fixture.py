"""Browser drives the real HTTP -> queue -> lifecycle path; only OS/network are fixtures."""

import os
from pathlib import Path
import socket
import subprocess
from threading import Thread
from time import monotonic, sleep

import uvicorn

from app.core.security import COOKIE_NAME, issue_session_token
from tests.test_cf_tunnels import (
    FakeTunnelRuntime,
    attach_local_observation,
    make_panel,
)


def test_browser_cf_on_off(tmp_path, monkeypatch):
    panel = make_panel(tmp_path)
    attach_local_observation(panel, monkeypatch)
    runtime = FakeTunnelRuntime()
    panel.app.state.cf_service.runtime = runtime
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
                "CF_COOKIE_NAME": COOKIE_NAME,
                "CF_COOKIE_TOKEN": issue_session_token(),
            },
            capture_output=True,
            text=True,
            timeout=40,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
        assert [task.status for task in panel.store.list_all()] == ["succeeded"] * 3
        assert len(runtime.resources) == 2
        assert sum(resource["active"] for resource in runtime.resources.values()) == 1
        assert panel.containers[0].state == "running"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
