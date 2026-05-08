from pathlib import Path

from app.models.assets import AssetSnapshot
from app.services.asset_policies import AssetPolicyService


def _write_rules(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_node_policy_blocks_agent_cli_packages(tmp_path: Path) -> None:
    _write_rules(
        tmp_path,
        'rules/node-packages.yaml',
        'packages:\n'
        '  - name: "@openai/codex"\n'
        '    managed_by: agent_cli\n'
        '    protected: true\n',
    )
    _write_rules(tmp_path, 'rules/python-packages.yaml', 'packages: []\n')
    service = AssetPolicyService(tmp_path)
    asset = AssetSnapshot(
        object_id='node:codex',
        category='node',
        name='@openai/codex',
        status='installed',
        current_version='0.128.0',
        metadata={'package_manager': 'npm'},
    )

    decided = service.apply(asset)

    assert decided.actionable is False
    assert decided.supports_actions == []
    assert decided.managed_by == 'agent_cli'
    assert decided.blocked_reason == '保留给 agent cli'


def test_python_policy_allows_only_whitelisted_packages(tmp_path: Path) -> None:
    _write_rules(tmp_path, 'rules/node-packages.yaml', 'packages: []\n')
    _write_rules(
        tmp_path,
        'rules/python-packages.yaml',
        'packages:\n'
        '  - name: "fastapi"\n'
        '    managed_by: python\n'
        '    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
    )
    service = AssetPolicyService(tmp_path)
    allowed = AssetSnapshot(
        object_id='python:fastapi',
        category='python',
        name='fastapi',
        status='installed',
        current_version='0.115.0',
        metadata={'package_manager': 'pip'},
    )
    blocked = AssetSnapshot(
        object_id='python:requests',
        category='python',
        name='requests',
        status='installed',
        current_version='2.32.0',
        metadata={'package_manager': 'pip'},
    )

    assert service.apply(allowed).actionable is True
    denied = service.apply(blocked)
    assert denied.actionable is False
    assert denied.blocked_reason == '白名单外'
    assert denied.supports_actions == []
