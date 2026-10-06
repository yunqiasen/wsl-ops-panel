from pathlib import Path
import re

import yaml

from app.models.assets import AssetSnapshot
from app.models.policies import PackageRule, PackageRuleSet

NODE_ACTIONS = ['update_latest', 'deploy_version', 'delete', 'full_delete']
PYTHON_ACTIONS = ['update_latest', 'deploy_version', 'delete', 'full_delete']


class AssetPolicyService:
    def __init__(self, config_root: Path | str) -> None:
        root = Path(config_root)
        self._node_rules = self._load_rules(root / 'rules' / 'node-packages.yaml')
        self._python_rules = {self._python_name(name): rule for name, rule in
                              self._load_rules(root / 'rules' / 'python-packages.yaml').items()}

    def apply(self, asset: AssetSnapshot) -> AssetSnapshot:
        if asset.category == 'node':
            return self._apply_node(asset)
        if asset.category == 'python':
            return self._apply_python(asset)
        return asset

    def _apply_node(self, asset: AssetSnapshot) -> AssetSnapshot:
        rule = self._node_rules.get(asset.name)
        if rule and (rule.protected or rule.managed_by not in {'node', 'agent'}):
            return asset.model_copy(
                update={
                    'actionable': False,
                    'supports_actions': [],
                    'managed_by': rule.managed_by,
                    'blocked_reason': rule.blocked_reason or ('受保护包' if rule.protected and rule.managed_by != 'agent_cli' else '保留给 agent cli'),
                    'policy_source': 'rules/node-packages.yaml',
                    'metadata': {**asset.metadata, 'full_delete_paths': rule.full_delete_paths},
                }
            )

        if rule and rule.managed_by == 'agent':
            allowed_actions = rule.allowed_actions or ['update_latest', 'deploy_version']
            return asset.model_copy(
                update={
                    'actionable': True,
                    'supports_actions': allowed_actions,
                    'managed_by': rule.managed_by,
                    'blocked_reason': rule.blocked_reason,
                    'policy_source': 'rules/node-packages.yaml',
                    'metadata': {**asset.metadata, 'full_delete_paths': rule.full_delete_paths},
                }
            )

        allowed_actions = rule.allowed_actions if rule and rule.allowed_actions else NODE_ACTIONS
        full_delete_paths = rule.full_delete_paths if rule else []
        return asset.model_copy(
            update={
                'actionable': True,
                'supports_actions': allowed_actions,
                'managed_by': 'node',
                'policy_source': 'rules/node-packages.yaml',
                'metadata': {**asset.metadata, 'full_delete_paths': full_delete_paths},
            }
        )

    def _apply_python(self, asset: AssetSnapshot) -> AssetSnapshot:
        rule = self._python_rules.get(self._python_name(asset.name))
        if rule is None:
            return asset.model_copy(
                update={
                    'actionable': False,
                    'supports_actions': [],
                    'managed_by': 'python',
                    'blocked_reason': '白名单外',
                    'policy_source': 'rules/python-packages.yaml',
                }
            )

        if rule.protected:
            return asset.model_copy(update={
                'actionable': False, 'supports_actions': [],
                'blocked_reason': rule.blocked_reason or '受保护包',
                'managed_by': rule.managed_by, 'policy_source': 'rules/python-packages.yaml',
            })

        return asset.model_copy(
            update={
                'actionable': True,
                'supports_actions': rule.allowed_actions or PYTHON_ACTIONS,
                'managed_by': rule.managed_by,
                'policy_source': 'rules/python-packages.yaml',
                'metadata': {**asset.metadata, 'full_delete_paths': rule.full_delete_paths},
            }
        )

    @staticmethod
    def _python_name(name: str) -> str:
        return re.sub(r'[-_.]+', '-', name).lower()

    def install_only(self, tool_type: str, package_name: str, action: str) -> bool:
        # Unmanaged Python distributions may be newly installed, not upgraded.
        return (tool_type == 'python' and action == 'install_or_update'
                and self._python_name(package_name) not in self._python_rules)

    def check_package_action(
        self, tool_type: str, package_name: str, action: str, *, version: str = '',
    ) -> tuple[bool, str | None]:
        if tool_type not in {'node', 'python'} or action not in {'install_or_update', 'delete'}:
            return False, '不支持的包操作'
        if self.install_only(tool_type, package_name, action):
            # The executor must check absence in the selected target interpreter.
            return True, None
        asset = self.apply(AssetSnapshot(object_id=f'{tool_type}__{package_name}',
                           category=tool_type, name=package_name, status='installed'))
        if not asset.actionable:
            return False, asset.blocked_reason
        required = ('deploy_version' if version and version != 'latest' else 'update_latest') if action == 'install_or_update' else 'delete'
        if required not in asset.supports_actions:
            return False, f'该包未开放 {required} 操作'
        return True, None

    @staticmethod
    def _load_rules(path: Path) -> dict[str, PackageRule]:
        payload = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {'packages': []}
        rules = PackageRuleSet.model_validate(payload)
        return {rule.name: rule for rule in rules.packages}
