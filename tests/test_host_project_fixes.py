"""Exercise real ownership capture and execution, not port-to-name guesses."""

import json
import subprocess
import sys

import pytest

from app.scanners.host_process_scanner import parse_listening_socket
from app.services.host_ownership import attach_owner, execute
from app.services.capabilities import enrich_asset_capabilities


def proc_at(tmp_path, name="docker-proxy", cgroup="0::/\n"):
    root = tmp_path / "proc"
    pid = root / "123"
    pid.mkdir(parents=True)
    (pid / "comm").write_text(name + "\n")
    (pid / "stat").write_text(f"123 ({name}) S " + "0 " * 18 + "555 0\n")
    (pid / "cgroup").write_text(cgroup)
    (pid / "cmdline").write_bytes(
        b"docker-proxy\0-container-ip\0172.19.0.2\0-container-port\08080\0"
    )
    # Avoid octal escapes adjacent to numeric command-line arguments.
    (pid / "cmdline").write_bytes(
        b"\0".join(
            [
                b"docker-proxy",
                b"-container-ip",
                b"172.19.0.2",
                b"-container-port",
                b"8080",
            ]
        )
    )
    return root


def docker_item(name="actual-owner", identity="a" * 64):
    return {
        "Id": identity,
        "Name": "/" + name,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "fixture",
                "com.docker.compose.project.working_dir": "/srv/fixture",
            }
        },
        "NetworkSettings": {
            "Networks": {"fixture": {"IPAddress": "172.19.0.2"}},
            "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8317"}]},
        },
    }


RAW = 'LISTEN 0 128 0.0.0.0:8317 0.0.0.0:* users:(("docker-proxy",pid=123,fd=5))'


def runner_for(containers, calls):
    def run(command, **kwargs):
        calls.append(command)
        if command[:1] == ["ss"]:
            out = RAW
        elif command[:2] == ["docker", "ps"]:
            out = "\n".join(c["Id"] for c in containers)
        else:
            out = json.dumps(containers)
        return subprocess.CompletedProcess(command, 0, out, "")

    return run


def test_host_uses_inspected_owner_not_known_port_hint(tmp_path, monkeypatch):
    proc = proc_at(tmp_path)
    asset = attach_owner(
        parse_listening_socket(RAW), containers=[docker_item()], proc_root=proc
    )
    assert asset.metadata["target_container_name"] == "actual-owner"
    assert "stop" in enrich_asset_capabilities(asset).supports_actions
    executed = []
    monkeypatch.setattr(
        "app.services.docker_lifecycle.execute",
        lambda context, action, **kw: executed.append((context, action)),
    )
    execute(
        asset.metadata["owner_snapshot"],
        "stop",
        runner=runner_for([docker_item()], []),
        proc_root=proc,
    )
    assert executed[0][0]["primary_container"] == "actual-owner"
    assert executed[0][1] == "stop"


def test_container_replacement_after_enqueue_is_rejected(tmp_path, monkeypatch):
    proc = proc_at(tmp_path)
    snapshot = attach_owner(
        parse_listening_socket(RAW), containers=[docker_item()], proc_root=proc
    ).metadata["owner_snapshot"]
    monkeypatch.setattr(
        "app.services.docker_lifecycle.execute",
        lambda *a, **kw: pytest.fail("changed container mutated"),
    )
    with pytest.raises(ValueError, match="归属已变化"):
        execute(
            snapshot,
            "stop",
            runner=runner_for([docker_item(identity="b" * 64)], []),
            proc_root=proc,
        )


def test_unknown_proxy_and_missing_process_have_no_buttons(tmp_path):
    proc = proc_at(tmp_path)
    asset = attach_owner(parse_listening_socket(RAW), proc_root=proc)
    assert enrich_asset_capabilities(asset).supports_actions == []
    assert enrich_asset_capabilities(parse_listening_socket(RAW)).supports_actions == []


def test_host_systemd_comes_from_cgroup_not_port(tmp_path):
    proc = proc_at(
        tmp_path,
        name="python3",
        cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/actual.service\n",
    )
    raw = RAW.replace("docker-proxy", "python3")
    asset = attach_owner(parse_listening_socket(raw), proc_root=proc)
    assert asset.metadata["target_unit_name"] == "actual.service"
    assert asset.metadata["owner_snapshot"]["service_scope"] == "user"
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            3 if "is-active" in command else 0,
            raw if command[0] == "ss" else "inactive\n",
            "",
        )

    execute(asset.metadata["owner_snapshot"], "stop", runner=run, proc_root=proc)
    assert ["systemctl", "--user", "stop", "actual.service"] in calls
    assert all("wsl-ops-panel.service" not in c for c in calls)


def test_actual_child_listener_stops_via_pinned_process_handle(tmp_path):
    code = "import socket,time;s=socket.socket();s.bind(('127.0.0.1',0));s.listen();print(s.getsockname()[1],flush=True);time.sleep(60)"
    child = subprocess.Popen(
        [sys.executable, "-c", code], stdout=subprocess.PIPE, text=True
    )
    try:
        port = child.stdout.readline().strip()
        result = subprocess.run(
            ["ss", "-ltnp"], capture_output=True, text=True, check=True, timeout=5
        )
        line = next(
            line for line in result.stdout.splitlines() if f"127.0.0.1:{port} " in line
        )
        asset = attach_owner(parse_listening_socket(line))
        assert asset.metadata["owner_type"] == "process"
        execute(asset.metadata["owner_snapshot"], "stop")
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_container_cgroup_is_not_treated_as_standalone_pid(tmp_path):
    proc = proc_at(
        tmp_path,
        name="python3",
        cgroup="0::/system.slice/docker-" + "a" * 64 + ".scope\n",
    )
    asset = attach_owner(
        parse_listening_socket(RAW.replace("docker-proxy", "python3")), proc_root=proc
    )
    assert asset.metadata["owner_type"] != "process"
    assert enrich_asset_capabilities(asset).supports_actions == []


@pytest.mark.parametrize('compose', [True, False])
def test_host_docker_stop_runs_real_lifecycle_with_captured_context(tmp_path, compose):
    proc = proc_at(tmp_path)
    container = docker_item()
    container['State'] = {'Running': True}
    if compose:
        container['Config']['Labels']['com.docker.compose.service'] = 'web'
    else:
        container['Config']['Labels'] = {}
    asset = attach_owner(parse_listening_socket(RAW), containers=[container], proc_root=proc)
    calls = []
    read = runner_for([container], calls)

    def runner(command, **kwargs):
        if command[:2] == ['docker', 'stop']:
            container['State']['Running'] = False
        return read(command, **kwargs)

    execute(asset.metadata['owner_snapshot'], 'stop', runner=runner, proc_root=proc)
    assert ['docker', 'stop', container['Id']] in calls
    assert not container['State']['Running']


def test_host_foreign_user_service_is_observed_without_current_user_controls(tmp_path):
    import os

    uid = os.getuid() + 10000
    proc = proc_at(tmp_path, name='python3',
                   cgroup=f'0::/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/web.service\n')
    asset = attach_owner(parse_listening_socket(RAW.replace('docker-proxy', 'python3')), proc_root=proc)
    assert asset.metadata['owner_type'] == 'unknown'
    assert enrich_asset_capabilities(asset).supports_actions == []
