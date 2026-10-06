"""The shared recovery journal needs a whole-transaction, cross-client lock."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.services.agent_router_config import AgentRouterConfigStore
from app.services.agent_router_control import AgentRouterController


def controllers(tmp_path, monkeypatch):
    home = tmp_path / "home"
    originals = {"codex": 'model="fixture"\n', "claude": '{"env":{}}\n'}
    paths = {}
    for client_id, (dirname, env, filename) in {
        "codex": (".codex", "CODEX_HOME", "config.toml"),
        "claude": (".claude", "CLAUDE_CONFIG_DIR", "settings.json"),
    }.items():
        root = home / dirname
        root.mkdir(parents=True)
        monkeypatch.setenv(env, str(root))
        paths[client_id] = root / filename
        paths[client_id].write_text(originals[client_id])
    monkeypatch.setenv("HOME", str(home))
    values = [
        AgentRouterController(
            AgentRouterConfigStore(tmp_path / "data"),
            home=home,
            health_probe=lambda: {"status": "ok"},
            runner=lambda _: 0,
        )
        for _ in range(2)
    ]
    return values, paths, originals


@pytest.mark.parametrize("boundary", ["controller", "journal"])
@pytest.mark.parametrize(
    "actions", [("enable", "enable"), ("disable", "disable"), ("disable", "enable")]
)
def test_cross_client_journal_transactions_serialize(
    tmp_path, monkeypatch, actions, boundary
):
    (a, b), paths, originals = controllers(tmp_path, monkeypatch)
    for controller, client_id, action in zip((a, b), ("codex", "claude"), actions):
        if action == "disable":
            controller.enable_takeover(client_id)

    def invoke(controller, action, client_id):
        if boundary == "controller":
            return getattr(controller, f"{action}_takeover")(client_id)["result"]
        if action == "enable":
            return controller.takeover.enable(
                client_id, f"http://127.0.0.1:7888/{client_id}/v1"
            )
        return controller.takeover.disable(client_id)

    first_read, release, second_attempt, second_read = (Event() for _ in range(4))
    read_a, read_b = a.takeover._read_state, b.takeover._read_state

    def paused_read():
        value = read_a()
        first_read.set()
        assert release.wait(5), "test did not release first transaction"
        return value

    def observed_read():
        second_read.set()
        return read_b()

    monkeypatch.setattr(a.takeover, "_read_state", paused_read)
    monkeypatch.setattr(b.takeover, "_read_state", observed_read)

    def second():
        second_attempt.set()
        return invoke(b, actions[1], "claude")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(invoke, a, actions[0], "codex")
        assert first_read.wait(3)
        other = pool.submit(second)
        try:
            assert second_attempt.wait(3)
            entered_early = second_read.wait(0.2)
        finally:
            release.set()
        assert first.result(timeout=5)["verified"]
        assert other.result(timeout=5)["verified"]
    assert not entered_early, "second client read the journal during first transaction"
    expected = {
        client
        for client, action in zip(("codex", "claude"), actions)
        if action == "enable"
    }
    assert set(a.takeover.status()) == expected
    if boundary == "controller":
        assert {
            key for key, enabled in a.store.snapshot()["takeover"].items() if enabled
        } == expected
    for client_id in expected:
        invoke(a, "disable", client_id)
    assert a.takeover.status() == {}
    for client_id, path in paths.items():
        assert path.read_text() == originals[client_id]
    assert json.loads(a.takeover.state_path.read_text()) == {}


def test_stop_restores_all_recovery_records(tmp_path, monkeypatch):
    (a, b), paths, originals = controllers(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(a.enable_takeover, "codex"),
            pool.submit(b.enable_takeover, "claude"),
        ]
        for future in futures:
            assert future.result(timeout=5)["result"]["verified"]
    result = a.stop(restore_clients=True)
    assert result["ok"]
    assert result["restored_clients"] == ["claude", "codex"]
    assert a.takeover.status() == {}
    assert all(
        path.read_text() == originals[client_id] for client_id, path in paths.items()
    )


def test_journal_lock_also_covers_independent_processes(tmp_path, monkeypatch):
    import subprocess
    import sys
    import time
    from app.services.agent_native_lock import exclusive_file_lock

    (a, _), paths, originals = controllers(tmp_path, monkeypatch)
    a.enable_takeover("codex")
    started = tmp_path / "started"
    script = """
import sys
from pathlib import Path
from app.services.agent_route_takeover import AgentRouteTakeover
home, state, started = map(Path, sys.argv[1:])
manager = AgentRouteTakeover(home, state)
started.write_text('ready')
manager.enable('claude', 'http://127.0.0.1:7888/claude')
"""
    process = None
    try:
        with exclusive_file_lock(a.takeover.state_root / ".route-takeover.lock"):
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(a.home),
                    str(a.takeover.state_root),
                    str(started),
                ]
            )
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.exists()
            time.sleep(0.15)
            assert process.poll() is None
            assert paths["claude"].read_text() == originals["claude"]
        assert process.wait(timeout=5) == 0
        assert set(a.takeover.status()) == {"codex", "claude"}
        a.stop(restore_clients=True)
        assert all(path.read_text() == originals[key] for key, path in paths.items())
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


def test_stop_blocks_new_takeovers_until_health_is_rechecked(tmp_path, monkeypatch):
    from app.services.agent_router_control import AgentRouterControlError

    (a, b), paths, originals = controllers(tmp_path, monkeypatch)
    stopping, release, attempted, probed = (Event() for _ in range(4))

    def stop_service(_):
        stopping.set()
        assert release.wait(5)
        return 0

    def health():
        probed.set()
        return {"status": "stopped"}

    a._runner = stop_service
    b._health_probe = health

    def enable():
        attempted.set()
        return b.enable_takeover("claude")

    with ThreadPoolExecutor(max_workers=2) as pool:
        stop = pool.submit(a.stop, restore_clients=True)
        assert stopping.wait(3)
        pending = pool.submit(enable)
        try:
            assert attempted.wait(3)
            early_probe = probed.wait(0.2)
        finally:
            release.set()
        assert stop.result(timeout=5)["ok"]
        with pytest.raises(AgentRouterControlError, match="not healthy"):
            pending.result(timeout=5)
    assert not early_probe
    assert a.takeover.status() == {}
    assert paths["claude"].read_text() == originals["claude"]
