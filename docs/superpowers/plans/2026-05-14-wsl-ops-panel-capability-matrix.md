# WSL Ops Panel Capability Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a category-aware capability matrix so Docker and Project get full runtime/CF/notify/autostart controls, while systemd/Node/Python/Host/System expose only sensible actions.

**Architecture:** Keep existing `AssetSnapshot.supports_actions` as the execution contract and add one small capability policy module that enriches assets with normalized `metadata.capabilities`. Category pages render action buttons from policy data instead of hardcoded buttons. Execution continues to use the existing task queue and adapters; this pass focuses on correct visibility and safe capability routing, not full notification template editing.

**Tech Stack:** FastAPI, Jinja2, vanilla JavaScript, pytest, ruff.

---

## File map

- `app/services/capabilities.py` — new normalized capability/action policy for categories and assets.
- `app/services/assets.py` — call capability enrichment for Docker, Project, systemd, Node, Python, Host, and System assets.
- `app/scanners/project_scanner.py` — detect Project web/runtime hints and expose full-capability candidates.
- `app/templates/category.html` — render bulk buttons from `available_actions`; add asset-level `data-supported-actions`.
- `app/templates/asset_detail.html` — show normalized capability status for every category.
- `app/static/app.js` — filter bulk toolbar by selected assets and selected category action policy.
- `tests/test_capabilities.py` — capability matrix unit tests.
- `tests/test_project_scanner.py` — Project full capability detection tests.
- `tests/test_overview_routes.py` — category UI action filtering contract tests.
- `tests/test_bulk_actions.py` — reject unsupported category/action combinations.

---

### Task 1: Capability matrix tests

**Files:**
- Create: `tests/test_capabilities.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_capabilities.py`:

```python
from app.models.assets import AssetSnapshot
from app.services.capabilities import enrich_asset_capabilities, get_category_actions


def test_docker_and_project_can_have_full_web_runtime_capabilities() -> None:
    docker = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='cpa',
            category='docker',
            name='CPA',
            status='running',
            supports_actions=['update_latest', 'deploy_version', 'delete', 'full_delete'],
            metadata={'project_dir': '/srv/cpa', 'ports': '8317/tcp', 'web_ui': {'enabled': True}},
        )
    )
    project = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='project__demo',
            category='project',
            name='demo',
            status='present',
            metadata={'path': '/srv/demo', 'web_ui': {'enabled': True}, 'ports': '8080'},
        )
    )

    for asset in (docker, project):
        assert 'cf_refresh' in asset.supports_actions
        assert 'notify_send' in asset.supports_actions
        assert asset.metadata['capabilities']['cf_tunnel']['applicable'] is True
        assert asset.metadata['capabilities']['wechat_notify']['applicable'] is True


def test_node_python_and_system_do_not_get_cf_or_notify() -> None:
    node = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='node__codex',
            category='node',
            name='codex',
            status='installed',
            supports_actions=['update_latest', 'deploy_version', 'delete', 'full_delete'],
        )
    )
    python = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='python__fastapi',
            category='python',
            name='fastapi',
            status='installed',
            supports_actions=['update_latest', 'deploy_version', 'delete', 'full_delete'],
        )
    )
    system = enrich_asset_capabilities(
        AssetSnapshot(object_id='system__docker', category='system', name='Docker', status='present')
    )

    for asset in (node, python, system):
        assert 'cf_refresh' not in asset.supports_actions
        assert 'notify_send' not in asset.supports_actions
        assert asset.metadata['capabilities']['cf_tunnel']['applicable'] is False
        assert asset.metadata['capabilities']['wechat_notify']['applicable'] is False


def test_systemd_gets_runtime_and_autostart_but_not_cf_notify() -> None:
    asset = enrich_asset_capabilities(
        AssetSnapshot(
            object_id='systemd__panel',
            category='systemd',
            name='wsl-ops-panel.service',
            status='active',
            supports_actions=['delete'],
            metadata={'unit_name': 'wsl-ops-panel.service'},
        )
    )

    assert {'start', 'stop', 'restart', 'autostart_enable', 'autostart_disable', 'delete'} <= set(asset.supports_actions)
    assert 'cf_refresh' not in asset.supports_actions
    assert 'notify_send' not in asset.supports_actions
    assert asset.metadata['capabilities']['runtime_control']['applicable'] is True
    assert asset.metadata['capabilities']['autostart']['applicable'] is True


def test_category_actions_match_simplified_scope() -> None:
    assert 'notify-send' in [item['slug'] for item in get_category_actions('docker')]
    assert 'notify-send' in [item['slug'] for item in get_category_actions('project')]
    assert 'notify-send' not in [item['slug'] for item in get_category_actions('node')]
    assert 'cf-refresh' not in [item['slug'] for item in get_category_actions('python')]
    assert 'start' in [item['slug'] for item in get_category_actions('systemd')]
    assert get_category_actions('system') == []
```

- [ ] **Step 2: Verify RED**

Run:

```bash
./.venv/bin/pytest -q tests/test_capabilities.py
```

Expected: fails with `ModuleNotFoundError: No module named 'app.services.capabilities'`.

- [ ] **Step 3: Implement `app/services/capabilities.py`**

Create `app/services/capabilities.py`:

```python
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
        'update_latest', 'deploy_version', 'start', 'stop', 'restart', 'autostart_enable', 'autostart_disable',
        'cf_create', 'cf_refresh', 'cf_disable', 'notify_send', 'delete', 'full_delete',
    ],
    'project': [
        'update_latest', 'deploy_version', 'start', 'stop', 'restart', 'autostart_enable', 'autostart_disable',
        'cf_create', 'cf_refresh', 'cf_disable', 'notify_send', 'delete', 'full_delete',
    ],
    'systemd': ['start', 'stop', 'restart', 'autostart_enable', 'autostart_disable', 'delete'],
    'node': ['update_latest', 'deploy_version', 'delete', 'full_delete'],
    'python': ['update_latest', 'deploy_version', 'delete', 'full_delete'],
    'host': [],
    'system': [],
    'agent_cli': ['update_latest', 'deploy_version', 'delete', 'full_delete'],
    'agent': [],
}

CAPABILITY_KEYS = ('versioning', 'runtime_control', 'autostart', 'cf_tunnel', 'wechat_notify', 'delete_control')


def get_category_actions(category_id: str) -> list[ActionDef]:
    return [ACTION_DEFINITIONS[action].copy() for action in CATEGORY_ACTIONS.get(category_id, [])]


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
    if asset.category == 'system':
        return []
    if asset.category == 'host':
        return []
    if asset.category == 'systemd':
        return category_actions
    if asset.category == 'docker':
        actions = [action for action in category_actions if action not in {'cf_create', 'cf_refresh', 'cf_disable', 'notify_send'}]
        if _has_web_surface(asset):
            actions.extend(['cf_create', 'cf_refresh', 'cf_disable', 'notify_send'])
        return actions
    if asset.category == 'project':
        actions = [action for action in category_actions if action in {'update_latest', 'deploy_version', 'delete', 'full_delete'}]
        if _has_runtime_hint(asset):
            actions.extend(['start', 'stop', 'restart', 'autostart_enable', 'autostart_disable'])
        if _has_web_surface(asset):
            actions.extend(['cf_create', 'cf_refresh', 'cf_disable', 'notify_send'])
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
    return capabilities


def _has_runtime_hint(asset: AssetSnapshot) -> bool:
    return bool(
        asset.metadata.get('service_unit')
        or asset.metadata.get('start_command')
        or asset.metadata.get('package_scripts')
        or asset.metadata.get('web_ui')
    )


def _has_web_surface(asset: AssetSnapshot) -> bool:
    web_ui = asset.metadata.get('web_ui')
    if isinstance(web_ui, dict) and web_ui.get('enabled') is True:
        return True
    if asset.metadata.get('ports') or asset.metadata.get('port'):
        return True
    capabilities = asset.metadata.get('capabilities')
    if isinstance(capabilities, dict):
        cf_tunnel = capabilities.get('cf_tunnel')
        if isinstance(cf_tunnel, dict) and cf_tunnel.get('enabled'):
            return True
    return False


def _ordered_unique(actions: list[str], category_actions: list[str]) -> list[str]:
    allowed = set(category_actions)
    seen: set[str] = set()
    result: list[str] = []
    for action in category_actions:
        if action in actions and action in allowed and action not in seen:
            result.append(action)
            seen.add(action)
    return result
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
./.venv/bin/pytest -q tests/test_capabilities.py
```

Expected: all tests in `tests/test_capabilities.py` pass.

- [ ] **Step 5: Commit**

```bash
git add app/services/capabilities.py tests/test_capabilities.py
git commit -m "feat: add asset capability matrix"
```

---

### Task 2: Enrich assets and Project detection

**Files:**
- Modify: `app/services/assets.py`
- Modify: `app/scanners/project_scanner.py`
- Modify: `tests/test_project_scanner.py`
- Modify: `tests/test_docker_scanner.py`

- [ ] **Step 1: Write failing Project scanner test**

Append to `tests/test_project_scanner.py`:

```python

def test_scan_projects_detects_web_ui_and_runtime_hints(tmp_path: Path) -> None:
    project = tmp_path / 'web-project'
    project.mkdir()
    (project / 'package.json').write_text(
        '{"scripts":{"start":"vite --host 0.0.0.0 --port 5173"},"dependencies":{"vite":"latest"}}',
        encoding='utf-8',
    )
    (project / 'logs').mkdir()
    (project / 'logs' / 'cftunnel-domain.txt').write_text('https://demo.trycloudflare.com', encoding='utf-8')
    (project / 'scripts').mkdir()
    (project / 'scripts' / 'cftunnel-start.sh').write_text('#!/usr/bin/env bash\n', encoding='utf-8')

    assets = scan_projects(root=tmp_path)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.metadata['web_ui']['enabled'] is True
    assert asset.metadata['web_ui']['port'] == '5173'
    assert asset.metadata['start_command'] == 'vite --host 0.0.0.0 --port 5173'
    assert asset.metadata['capabilities']['cf_tunnel']['enabled'] is True
    assert asset.metadata['capabilities']['cf_tunnel']['current_url'] == 'https://demo.trycloudflare.com'
```

- [ ] **Step 2: Verify RED**

Run:

```bash
./.venv/bin/pytest -q tests/test_project_scanner.py::test_scan_projects_detects_web_ui_and_runtime_hints
```

Expected: fails because `web_ui`, `start_command`, and `capabilities` are missing.

- [ ] **Step 3: Implement Project detection**

Modify `app/scanners/project_scanner.py`:

- Import `json` and `re`.
- Add helpers `_read_package_scripts`, `_detect_web_ui`, and `_build_project_capabilities`.
- Add metadata keys `package_scripts`, `start_command`, `web_ui`, and `capabilities`.

Implementation detail:

```python
import json
import re
```

Inside metadata creation add:

```python
        package_scripts = _read_package_scripts(child)
        start_command = package_scripts.get('start')
        web_ui = _detect_web_ui(child, start_command=start_command)
        capabilities = _build_project_capabilities(child, web_ui=web_ui)
```

Add to metadata:

```python
                    'package_scripts': package_scripts,
                    'start_command': start_command,
                    'web_ui': web_ui,
                    'capabilities': capabilities,
```

Add helpers:

```python
_PORT_RE = re.compile(r'(?:--port\s+|PORT=)(\d{2,5})')


def _read_package_scripts(path: Path) -> dict[str, str]:
    package_json = path / 'package.json'
    if not package_json.exists():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}
    scripts = payload.get('scripts')
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items() if isinstance(value, str)}


def _detect_web_ui(path: Path, *, start_command: str | None) -> dict[str, object]:
    port = None
    if start_command:
        match = _PORT_RE.search(start_command)
        if match:
            port = match.group(1)
    enabled = bool(port or (path / 'scripts' / 'cftunnel-start.sh').exists())
    return {'enabled': enabled, 'port': port, 'url_path': '/'}


def _build_project_capabilities(path: Path, *, web_ui: dict[str, object]) -> dict[str, dict[str, object]]:
    cftunnel_script = path / 'scripts' / 'cftunnel-start.sh'
    domain_file = path / 'logs' / 'cftunnel-domain.txt'
    current_url = domain_file.read_text(encoding='utf-8', errors='replace').strip() if domain_file.exists() else None
    return {
        'cf_tunnel': {
            'enabled': cftunnel_script.exists(),
            'script_path': str(cftunnel_script) if cftunnel_script.exists() else None,
            'domain_file': str(domain_file),
            'current_url': current_url,
        },
        'wechat_notify': {'enabled': False},
        'runtime_control': {'enabled': bool(web_ui.get('enabled'))},
        'autostart': {'enabled': False},
    }
```

- [ ] **Step 4: Enrich all asset outputs**

Modify `app/services/assets.py`:

```python
from app.services.capabilities import enrich_asset_capabilities
```

Wrap each returned asset:

- Docker registered asset before append: `enrich_asset_capabilities(AssetSnapshot(...))`
- Runtime discovered Docker asset before append.
- systemd asset before append.
- Node/Python after policy apply: `enrich_asset_capabilities(self._policy_service.apply(asset))`
- Project/Host/System scanner output: `[enrich_asset_capabilities(asset) for asset in ...]`

- [ ] **Step 5: Update Docker scanner test expectation**

In `tests/test_docker_scanner.py`, update discovered action expectation to include `restart` if not already present:

```python
        'restart',
```

between `stop` and `autostart_enable`.

- [ ] **Step 6: Verify GREEN**

Run:

```bash
./.venv/bin/pytest -q tests/test_project_scanner.py tests/test_docker_scanner.py tests/test_capabilities.py
```

Expected: selected tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/services/assets.py app/scanners/project_scanner.py tests/test_project_scanner.py tests/test_docker_scanner.py
git commit -m "feat: enrich assets with category capabilities"
```

---

### Task 3: Category UI action filtering

**Files:**
- Modify: `app/api/overview.py`
- Modify: `app/templates/category.html`
- Modify: `app/static/app.js`
- Modify: `tests/test_overview_routes.py`

- [ ] **Step 1: Write failing UI tests**

Append to `tests/test_overview_routes.py`:

```python

def test_node_category_does_not_render_cf_or_notify_bulk_actions(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: update\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    asset = parse_npm_package('update@0.7.4')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    _login(client)

    response = client.get('/categories/node')

    assert response.status_code == 200
    assert 'data-bulk-action="update-latest"' in response.text
    assert 'data-bulk-action="deploy-version"' in response.text
    assert 'data-bulk-action="cf-refresh"' not in response.text
    assert 'data-bulk-action="notify-send"' not in response.text


def test_systemd_category_renders_runtime_actions_without_cf_notify(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'systemd.yaml', 'id: systemd\nlabel: systemd\norder: 20\nenabled: true\n')
    _write_registry_file(
        tmp_path,
        'objects',
        'panel.yaml',
        'id: panel\ncategory: systemd\ntype: systemd_unit\nname: panel\nconfig:\n  unit_name: wsl-ops-panel.service\n  working_dir: /srv/panel\n',
    )
    client = TestClient(create_app(config_root=tmp_path, systemd_scanner=lambda: []))
    _login(client)

    response = client.get('/categories/systemd')

    assert response.status_code == 200
    assert 'data-bulk-action="start"' in response.text
    assert 'data-bulk-action="stop"' in response.text
    assert 'data-bulk-action="autostart-enable"' in response.text
    assert 'data-bulk-action="cf-refresh"' not in response.text
    assert 'data-bulk-action="notify-send"' not in response.text
```

- [ ] **Step 2: Verify RED**

Run:

```bash
./.venv/bin/pytest -q tests/test_overview_routes.py::test_node_category_does_not_render_cf_or_notify_bulk_actions tests/test_overview_routes.py::test_systemd_category_renders_runtime_actions_without_cf_notify
```

Expected: fails because category template currently renders CF/notify for every category.

- [ ] **Step 3: Pass category actions into page context**

Modify `app/api/overview.py` category page handler:

```python
from app.services.capabilities import get_category_actions
```

Add to `build_page_context(...):`

```python
available_actions=get_category_actions(category_id),
```

- [ ] **Step 4: Render dynamic bulk buttons**

Replace hardcoded buttons in `app/templates/category.html` with:

```html
    {% for action in available_actions %}
    <button class="{{ action.style }}-button" type="button" data-bulk-action="{{ action.slug }}">{{ action.label }}</button>
    {% endfor %}
```

Add supported action data to each asset card:

```html
  <li class="asset-card" data-asset-card data-asset-id="{{ asset.object_id }}" data-supported-actions="{{ asset.supports_actions|join(',') }}">
```

- [ ] **Step 5: Filter selected bulk actions in JS**

In `app/static/app.js`, inside `syncUI()`, after count update add:

```javascript
      const selectedActions = assetIdsForSelection().map((assetId) => {
        const card = document.querySelector(`[data-asset-id="${CSS.escape(assetId)}"]`);
        return new Set((card?.dataset.supportedActions || '').split(',').filter(Boolean));
      });
      toolbar.querySelectorAll('[data-bulk-action]').forEach((actionButton) => {
        const action = actionSlugToName(actionButton.dataset.bulkAction);
        const allowed = selectedActions.length === 0 || selectedActions.every((actions) => actions.has(action));
        actionButton.hidden = !allowed;
      });
```

Add helpers above `syncUI()`:

```javascript
    function assetIdsForSelection() {
      return Array.from(selected);
    }

    function actionSlugToName(slug) {
      return {
        'update-latest': 'update_latest',
        'deploy-version': 'deploy_version',
        'full-delete': 'full_delete',
        'autostart-enable': 'autostart_enable',
        'autostart-disable': 'autostart_disable',
        'cf-create': 'cf_create',
        'cf-refresh': 'cf_refresh',
        'cf-disable': 'cf_disable',
        'notify-send': 'notify_send',
      }[slug] || slug;
    }
```

- [ ] **Step 6: Verify GREEN**

Run:

```bash
./.venv/bin/pytest -q tests/test_overview_routes.py
```

Expected: overview route tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/api/overview.py app/templates/category.html app/static/app.js tests/test_overview_routes.py
git commit -m "feat: filter category bulk actions by capability"
```

---

### Task 4: Enforce unsupported bulk actions server-side

**Files:**
- Modify: `app/api/assets.py`
- Modify: `tests/test_bulk_actions.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_bulk_actions.py`:

```python

def test_bulk_cf_action_rejects_node_asset_even_if_endpoint_exists(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: update\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    from app.scanners.node_scanner import parse_npm_package

    asset = parse_npm_package('update@0.7.4')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [asset], task_store=InMemoryTaskStore()))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post('/api/bulk/actions/cf-refresh', json={'asset_ids': [asset.object_id]})

    assert response.status_code == 400
    assert response.json()['detail'] == f'action cf_refresh is not supported by {asset.object_id}'
```

- [ ] **Step 2: Verify RED**

Run:

```bash
./.venv/bin/pytest -q tests/test_bulk_actions.py::test_bulk_cf_action_rejects_node_asset_even_if_endpoint_exists
```

Expected: currently returns another error or reaches adapter, not the explicit unsupported action error.

- [ ] **Step 3: Add support check in `enqueue_asset_action`**

In `app/api/assets.py`, before building adapter plan:

```python
    if action not in asset.supports_actions:
        raise HTTPException(status_code=400, detail=f'action {action} is not supported by {object_id}')
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
./.venv/bin/pytest -q tests/test_bulk_actions.py
```

Expected: bulk action tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/api/assets.py tests/test_bulk_actions.py
git commit -m "fix: reject unsupported asset actions"
```

---

### Task 5: Systemd runtime actions

**Files:**
- Modify: `app/adapters/systemd_adapter.py`
- Modify: `tests/test_systemd_adapter.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_systemd_adapter.py`:

```python

def test_systemd_adapter_plans_runtime_and_autostart_actions() -> None:
    adapter = SystemdUnitAdapter(unit_name='wsl-ops-panel.service', working_dir='/srv/panel')

    assert adapter.plan_action('start').commands == [['sudo', 'systemctl', 'start', 'wsl-ops-panel.service']]
    assert adapter.plan_action('stop').commands == [['sudo', 'systemctl', 'stop', 'wsl-ops-panel.service']]
    assert adapter.plan_action('restart').commands == [['sudo', 'systemctl', 'restart', 'wsl-ops-panel.service']]
    assert adapter.plan_action('autostart_enable').commands == [['sudo', 'systemctl', 'enable', 'wsl-ops-panel.service']]
    assert adapter.plan_action('autostart_disable').commands == [['sudo', 'systemctl', 'disable', 'wsl-ops-panel.service']]
```

- [ ] **Step 2: Verify RED**

Run:

```bash
./.venv/bin/pytest -q tests/test_systemd_adapter.py::test_systemd_adapter_plans_runtime_and_autostart_actions
```

Expected: fails because only delete is supported.

- [ ] **Step 3: Implement systemd runtime actions**

Modify `app/adapters/systemd_adapter.py`:

```python
SystemdAction = Literal['start', 'stop', 'restart', 'autostart_enable', 'autostart_disable', 'delete', 'full_delete']
```

Add before delete logic:

```python
        command_map = {
            'start': ['sudo', 'systemctl', 'start', self.unit_name],
            'stop': ['sudo', 'systemctl', 'stop', self.unit_name],
            'restart': ['sudo', 'systemctl', 'restart', self.unit_name],
            'autostart_enable': ['sudo', 'systemctl', 'enable', self.unit_name],
            'autostart_disable': ['sudo', 'systemctl', 'disable', self.unit_name],
        }
        if action in command_map:
            return ActionPlan(
                commands=[command_map[action]],
                working_dir=self.working_dir,
                requires_sudo=True,
                preview_objects=[self.unit_name],
                preview_paths=[self.working_dir],
            )
```

- [ ] **Step 4: Verify GREEN**

Run:

```bash
./.venv/bin/pytest -q tests/test_systemd_adapter.py
```

Expected: systemd adapter tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/adapters/systemd_adapter.py tests/test_systemd_adapter.py
git commit -m "feat: add systemd runtime action plans"
```

---

### Task 6: Full verification and service restart

**Files:**
- No planned code changes.

- [ ] Run lint:

```bash
./.venv/bin/ruff check app tests
```

Expected: `All checks passed!`

- [ ] Run tests:

```bash
./.venv/bin/pytest -q
```

Expected: all tests pass.

- [ ] Restart service:

```bash
sudo systemctl restart wsl-ops-panel.service
```

- [ ] Verify health:

```bash
curl -sS http://127.0.0.1:8328/healthz
curl -sS http://100.126.43.55:8328/healthz
```

Expected for both: `{"status":"ok"}`.

- [ ] Push branch:

```bash
git push origin phase1-complete
```

---

## Self-review

Spec coverage:

- Docker and Project full capabilities: Task 1 + Task 2.
- Other categories simplified: Task 1 + Task 3 + Task 5.
- CF/notify only for web/business-style assets: Task 1 `_has_web_surface`, Task 2 Project detection.
- UI button filtering: Task 3.
- Server-side safety: Task 4.
- systemd start/stop/autostart: Task 5.

Scope intentionally excluded:

- Full notification template editor.
- Actual generation of CF scripts and systemd units.
- Killing host processes.
- Agent-specific extension management.
