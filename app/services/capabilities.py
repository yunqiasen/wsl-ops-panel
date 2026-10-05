from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.models.assets import AssetSnapshot

ActionDef = dict[str, str]

ACTION_DEFINITIONS: dict[str, ActionDef] = {
    'update_latest': {'slug': 'update-latest', 'label': '更新最新版', 'style': 'primary'},
    'deploy_version': {'slug': 'deploy-version', 'label': '部署选定版本', 'style': 'ghost'},
    'start': {'slug': 'start', 'label': '开启', 'style': 'ghost'},
    'stop': {'slug': 'stop', 'label': '关闭', 'style': 'ghost'},
    'restart': {'slug': 'restart', 'label': '重启', 'style': 'ghost'},
    'autostart_enable': {'slug': 'autostart-enable', 'label': '开启自启', 'style': 'ghost'},
    'autostart_disable': {'slug': 'autostart-disable', 'label': '关闭自启', 'style': 'ghost'},
    'cf_create': {'slug': 'cf-create', 'label': '生成 CF', 'style': 'ghost'},
    'cf_refresh': {'slug': 'cf-refresh', 'label': '刷新 CF', 'style': 'ghost'},
    'cf_disable': {'slug': 'cf-disable', 'label': '关闭 CF', 'style': 'ghost'},
    'notify_send': {'slug': 'notify-send', 'label': '发送微信通知', 'style': 'ghost'},
    'delete': {'slug': 'delete', 'label': '删除', 'style': 'danger'},
    'full_delete': {'slug': 'full-delete', 'label': '完全删除', 'style': 'danger'},
}

CATEGORY_ACTIONS: dict[str, list[str]] = {
    'docker': [
        'update_latest',
        'deploy_version',
        'start',
        'stop',
        'restart',
        'autostart_enable',
        'autostart_disable',
        'cf_create',
        'cf_refresh',
        'cf_disable',
        'notify_send',
        'delete',
        'full_delete',
    ],
    'project': [
        'update_latest',
        'deploy_version',
        'start',
        'stop',
        'restart',
        'autostart_enable',
        'autostart_disable',
        'cf_create',
        'cf_refresh',
        'cf_disable',
        'notify_send',
        'delete',
        'full_delete',
    ],
    'systemd': ['start', 'stop', 'restart', 'autostart_enable', 'autostart_disable', 'delete'],
    'node': ['update_latest', 'deploy_version', 'delete', 'full_delete'],
    'python': ['update_latest', 'deploy_version', 'delete', 'full_delete'],
    'host': ['start', 'stop'],
    'system': ['update_latest', 'deploy_version'],
    'agent_cli': ['update_latest', 'deploy_version', 'delete', 'full_delete'],
    'agent': [],
    'remote': [],
}

CAPABILITY_KEYS = ('versioning', 'runtime_control', 'autostart', 'cf_tunnel', 'wechat_notify', 'delete_control', 'repo_metadata')


def get_category_actions(category_id: str) -> list[ActionDef]:
    return [ACTION_DEFINITIONS[action].copy() for action in CATEGORY_ACTIONS.get(category_id, [])]


def get_actions_for_assets(category_id: str, assets: list[AssetSnapshot]) -> list[ActionDef]:
    category_actions = CATEGORY_ACTIONS.get(category_id, [])
    supported_actions: set[str] = set()
    for asset in assets:
        supported_actions.update(asset.supports_actions)
    return [ACTION_DEFINITIONS[action].copy() for action in category_actions if action in supported_actions]


def enrich_asset_capabilities(asset: AssetSnapshot) -> AssetSnapshot:
    category_actions = CATEGORY_ACTIONS.get(asset.category, [])
    existing_actions = list(asset.supports_actions)
    inferred_actions = _infer_actions(asset, category_actions)
    merged_actions = _ordered_unique([*existing_actions, *inferred_actions], category_actions)

    existing_capabilities = asset.metadata.get('capabilities') if isinstance(asset.metadata.get('capabilities'), dict) else {}
    capabilities = _build_capabilities(asset, merged_actions, existing_capabilities)
    metadata = {**asset.metadata, 'capabilities': capabilities}
    return asset.model_copy(update={'supports_actions': merged_actions, 'metadata': metadata})


def _infer_actions(asset: AssetSnapshot, category_actions: list[str]) -> list[str]:
    if asset.category in {'node', 'python', 'agent_cli'}:
        return [action for action in category_actions if action in set(asset.supports_actions)]
    if asset.category == 'remote':
        return []
    if asset.category == 'system':
        return [action for action in category_actions if action in set(asset.supports_actions)]
    if asset.category == 'host':
        owner_type = asset.metadata.get('owner_type')
        target_unit_name = asset.metadata.get('target_unit_name')
        target_container_name = asset.metadata.get('target_container_name')
        pid = asset.metadata.get('pid')
        if owner_type == 'docker' and isinstance(target_container_name, str) and target_container_name:
            return [action for action in category_actions if action in {'start', 'stop'}]
        if owner_type == 'systemd' and isinstance(target_unit_name, str) and target_unit_name:
            return [action for action in category_actions if action in {'start', 'stop'}]
        if owner_type == 'process' and isinstance(pid, int):
            return [action for action in category_actions if action == 'stop']
        return []
    if asset.category == 'systemd':
        return category_actions
    if asset.category == 'docker':
        actions = [action for action in category_actions if action not in {'cf_create', 'cf_refresh', 'cf_disable', 'notify_send'}]
        actions.extend(_supported_cf_actions(asset))
        if _has_web_surface(asset):
            actions.append('notify_send')
        return actions
    if asset.category == 'project':
        actions = [action for action in category_actions if action in {'update_latest', 'deploy_version', 'delete', 'full_delete'}]
        if _has_runtime_hint(asset):
            actions.extend(['start', 'stop', 'restart', 'autostart_enable', 'autostart_disable'])
        actions.extend(_supported_cf_actions(asset))
        if _has_web_surface(asset):
            actions.append('notify_send')
        return actions
    return [action for action in category_actions if action in set(asset.supports_actions)]


def _build_capabilities(
    asset: AssetSnapshot,
    actions: list[str],
    existing_capabilities: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    for key in CAPABILITY_KEYS:
        existing = deepcopy(existing_capabilities.get(key, {})) if isinstance(existing_capabilities.get(key), dict) else {}
        capabilities[key] = {'applicable': False, 'enabled': False, 'supported_actions': [], **existing}

    capabilities['versioning'].update(
        {
            'applicable': any(action in actions for action in ('update_latest', 'deploy_version')),
            'enabled': any(action in actions for action in ('update_latest', 'deploy_version')),
            'supported_actions': [action for action in ('update_latest', 'deploy_version') if action in actions],
        }
    )
    capabilities['runtime_control'].update(
        {
            'applicable': any(action in actions for action in ('start', 'stop', 'restart')),
            'enabled': any(action in actions for action in ('start', 'stop', 'restart')),
            'supported_actions': [action for action in ('start', 'stop', 'restart') if action in actions],
        }
    )
    capabilities['autostart'].update(
        {
            'applicable': any(action in actions for action in ('autostart_enable', 'autostart_disable')),
            'enabled': bool(capabilities['autostart'].get('enabled')),
            'supported_actions': [action for action in ('autostart_enable', 'autostart_disable') if action in actions],
        }
    )
    capabilities['cf_tunnel'].update(
        {
            'applicable': any(action in actions for action in ('cf_create', 'cf_refresh', 'cf_disable')),
            'enabled': bool(capabilities['cf_tunnel'].get('enabled')),
            'supported_actions': [action for action in ('cf_create', 'cf_refresh', 'cf_disable') if action in actions],
        }
    )
    capabilities['wechat_notify'].update(
        {
            'applicable': 'notify_send' in actions,
            'enabled': bool(capabilities['wechat_notify'].get('enabled')),
            'supported_actions': ['notify_send'] if 'notify_send' in actions else [],
        }
    )
    capabilities['delete_control'].update(
        {
            'applicable': any(action in actions for action in ('delete', 'full_delete')),
            'enabled': any(action in actions for action in ('delete', 'full_delete')),
            'supported_actions': [action for action in ('delete', 'full_delete') if action in actions],
        }
    )
    capabilities['repo_metadata'].update(
        {
            'applicable': bool(asset.metadata.get('git_remote_url') or capabilities['repo_metadata'].get('enabled')),
            'enabled': bool(asset.metadata.get('git_remote_url') or capabilities['repo_metadata'].get('enabled')),
            'supported_actions': [],
        }
    )
    return capabilities


def _has_runtime_hint(asset: AssetSnapshot) -> bool:
    web_ui = asset.metadata.get('web_ui')
    return bool(
        asset.metadata.get('service_unit')
        or asset.metadata.get('start_command')
        or (isinstance(web_ui, dict) and web_ui.get('enabled') is True)
    )


def _has_web_surface(asset: AssetSnapshot) -> bool:
    web_ui = asset.metadata.get('web_ui')
    if isinstance(web_ui, dict) and web_ui.get('enabled') is True:
        return True
    if asset.metadata.get('ports') or asset.metadata.get('port'):
        return True
    runtime = asset.metadata.get('runtime')
    if isinstance(runtime, dict) and runtime.get('ports'):
        return True
    capabilities = asset.metadata.get('capabilities')
    if isinstance(capabilities, dict):
        cf_tunnel = capabilities.get('cf_tunnel')
        if isinstance(cf_tunnel, dict) and cf_tunnel.get('enabled'):
            return True
    return False


def _supported_cf_actions(asset: AssetSnapshot) -> list[str]:
    capabilities = asset.metadata.get('capabilities')
    if not isinstance(capabilities, dict):
        return []
    cf_tunnel = capabilities.get('cf_tunnel')
    if not isinstance(cf_tunnel, dict):
        return []
    supported = cf_tunnel.get('supported_actions')
    if isinstance(supported, list):
        return [action for action in ('cf_create', 'cf_refresh', 'cf_disable') if action in supported]
    if cf_tunnel.get('enabled') is True:
        return ['cf_create', 'cf_refresh', 'cf_disable']
    return []


def _ordered_unique(actions: list[str], category_actions: list[str]) -> list[str]:
    allowed = set(category_actions)
    seen: set[str] = set()
    result: list[str] = []
    for action in category_actions:
        if action in actions and action in allowed and action not in seen:
            result.append(action)
            seen.add(action)
    return result
