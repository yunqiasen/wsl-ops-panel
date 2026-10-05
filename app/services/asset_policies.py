from pathlib import Path

import yaml

from app.models.assets import AssetSnapshot
from app.models.policies import PackageRule, PackageRuleSet

NODE_ACTIONS = ['update_latest', 'deploy_version', 'delete', 'full_delete']
PYTHON_ACTIONS = ['update_latest', 'deploy_version', 'delete', 'full_delete']


class AssetPolicyService:
    def __init__(self, config_root: Path | str) -> None:
        root = Path(config_root)
        self._node_rules = self._load_rules(root / 'rules' / 'node-packages.yaml')
        self._python_rules = self._load_rules(root / 'rules' / 'python-packages.yaml')

    def apply(self, asset: AssetSnapshot) -> AssetSnapshot:
        if asset.category == 'node':
            return self._apply_node(asset)
        if asset.category == 'python':
            return self._apply_python(asset)
        return asset

    def _apply_node(self, asset: AssetSnapshot) -> AssetSnapshot:
        rule = self._node_rules.get(asset.name)
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
        if rule and (rule.protected or rule.managed_by != 'node'):
            return asset.model_copy(
                update={
                    'actionable': False,
                    'supports_actions': [],
                    'managed_by': rule.managed_by,
                    'blocked_reason': rule.blocked_reason or '保留给 agent cli',
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
        rule = self._python_rules.get(asset.name)
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
    def _load_rules(path: Path) -> dict[str, PackageRule]:
        payload = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {'packages': []}
        rules = PackageRuleSet.model_validate(payload)
        return {rule.name: rule for rule in rules.packages}
