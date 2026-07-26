from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_agent_router_installer_and_registry_use_same_system_service() -> None:
    script = (ROOT / "scripts/install_agent_router_service.sh").read_text(
        encoding="utf-8"
    )
    registered = yaml.safe_load(
        (ROOT / "config/objects/systemd-agent-router.yaml").read_text(encoding="utf-8")
    )

    assert 'SERVICE_NAME="wsl-agent-router.service"' in script
    assert ".venv/bin/python" in script
    assert "-m app.agent_router.main" in script
    assert "AGENT_ROUTER_CONFIG=" in script
    assert "Restart=on-failure" in script
    assert registered["config"]["unit_name"] == "wsl-agent-router.service"


def test_sudo_preflight_checks_router_lifecycle_commands() -> None:
    script = (ROOT / "scripts/check_sudo_rules.sh").read_text(encoding="utf-8")

    assert 'for action in start stop restart; do' in script
    assert 'sudo -n -l systemctl "$action" "$unit"' in script
