import os
from pathlib import Path

from app.services.system_overview import build_system_overview


def test_build_system_overview_is_lightweight_and_includes_proxy_status(monkeypatch) -> None:
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:7890')
    monkeypatch.setenv('NO_PROXY', 'localhost,127.0.0.1,host.docker.internal')
    monkeypatch.setattr(os, 'getloadavg', lambda: (0.1, 0.2, 0.3), raising=False)

    overview = build_system_overview(
        command_runner=lambda command, timeout=1.0: _fake_command(command),
        env=os.environ,
        proc_root=Path('/does/not/exist'),
    )

    assert overview['identity']['hostname'] == 'wsl-host'
    assert overview['network']['tailscale_ip'] == '100.126.43.55'
    assert overview['network']['tailscale_label'] == '当前 WSL Tailscale IP'
    assert overview['network']['panel_tailscale_description'] == '当前 WSL 面板的 Tailscale 内网访问地址'
    assert overview['network']['default_route'] == 'default via 172.22.64.1 dev eth0'
    assert overview['network']['wsl_ip'] == '172.22.70.10'
    assert overview['network']['windows_gateway'] == '172.22.64.1'
    assert overview['network']['host_docker_internal'] == '172.17.0.1'
    assert overview['proxy']['enabled'] is True
    assert overview['proxy']['http_proxy'] == 'http://127.0.0.1:7890'
    assert overview['proxy']['no_proxy_has_local'] is True
    assert overview['proxy']['clash_container_count'] == 2
    assert overview['proxy']['clash_proxy_range'] == '41001-41002'
    assert overview['proxy']['clash_control_range'] == '42001-42002'
    assert overview['proxy']['gap_mihomo'] == '127.0.0.1:41901, 172.17.0.1:41901'
    assert overview['ports']['total'] == 2
    assert {item['port'] for item in overview['ports']['items']} == {'8328', '7890'}
    assert overview['services']['docker'] == 'active'
    assert overview['services']['cloudflare_tunnel'] == 'active'


def _fake_command(command: list[str], timeout: float = 1.0) -> str:
    key = tuple(command)
    responses = {
        ('hostname',): 'wsl-host\n',
        ('uname', '-r'): '6.6.87.2-microsoft-standard-WSL2\n',
        ('tailscale', 'ip', '-4'): '100.126.43.55\n',
        ('tailscale', 'status', '--self', '--peers=false'): '100.126.43.55  win-20250727mef-1  user@  linux  -\n',
        ('ip', 'route', 'show', 'default'): 'default via 172.22.64.1 dev eth0 proto kernel\n',
        ('hostname', '-I'): '172.22.70.10 172.17.0.1\n',
        ('getent', 'hosts', 'host.docker.internal'): '172.17.0.1 host.docker.internal\n',
        ('systemctl', 'is-active', 'docker'): 'active\n',
        ('systemctl', 'is-active', 'wsl-xinghaihub-tunnel.service'): 'active\n',
        ('systemctl', 'is-active', 'wsl-ops-panel.service'): 'active\n',
        ('systemctl', 'is-active', 'tailscaled'): 'active\n',
        ('ss', '-ltnp'): '\n'.join(
            [
                'State Recv-Q Send-Q Local Address:Port Peer Address:Port Process',
                'LISTEN 0 2048 0.0.0.0:8328 0.0.0.0:* users:(("uvicorn",pid=1,fd=14))',
                'LISTEN 0 4096 127.0.0.1:7890 0.0.0.0:* users:(("mihomo",pid=2,fd=8))',
            ]
        ),
        ('docker', 'ps', '--format', '{{.Names}}\t{{.Ports}}'): '\n'.join(
            [
                'clash_1\t0.0.0.0:41001->7890/tcp, 0.0.0.0:42001->9090/tcp',
                'clash_2\t0.0.0.0:41002->7890/tcp, 0.0.0.0:42002->9090/tcp',
                'gap-mihomo\t127.0.0.1:41901->7890/tcp, 172.17.0.1:41901->7890/tcp, 127.0.0.1:42901->9090/tcp',
            ]
        ),
    }
    return responses.get(key, '')
