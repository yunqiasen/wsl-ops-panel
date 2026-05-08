# WSL Ops Panel Node + Python Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 WSL Ops Panel 的 `node` 与 `python` 分类从只读展示升级为可执行管理，并保持现有任务队列、后台 worker、日志和终端链路不变。

**Architecture:** 继续保留现有扫描器做对象发现，在扫描结果和动作执行之间新增策略层与版本源层。Node / Python 的资产先经过规则策略决策，得到可操作性、支持动作、只读原因和显式清理路径，再由各自适配器产出 `ActionPlan` 进入统一串行任务队列。

**Tech Stack:** Python 3.13、FastAPI、Pydantic v2、httpx、YAML、pytest、ruff、现有后台 task worker。

---

## File Structure

### Create

- `app/adapters/node_adapter.py`
- `app/adapters/python_adapter.py`
- `app/models/policies.py`
- `app/services/asset_policies.py`
- `app/services/package_versions.py`
- `config/rules/node-packages.yaml`
- `config/rules/python-packages.yaml`
- `tests/test_asset_policies.py`
- `tests/test_node_adapter.py`
- `tests/test_python_adapter.py`
- `tests/test_package_versions.py`
- `tests/test_package_asset_routes.py`

### Modify

- `app/api/assets.py`
- `app/main.py`
- `app/models/assets.py`
- `app/services/assets.py`
- `app/templates/asset_detail.html`
- `app/templates/category.html`
- `app/static/app.css`
- `tests/test_readonly_scanners.py`
- `tests/test_overview_routes.py`
- `README.md`
- `docs/operations.md`

### Responsibility split

- `app/models/policies.py`：规则与决策的数据模型
- `app/services/asset_policies.py`：Node / Python 包规则加载与策略决策
- `app/services/package_versions.py`：npm / PyPI 版本查询与失败状态包装
- `app/adapters/node_adapter.py`：Node 四控动作与版本列表
- `app/adapters/python_adapter.py`：Python 四控动作与版本列表
- `app/services/assets.py`：把扫描结果接入策略层
- `app/api/assets.py`：把 Node / Python 资产接入动作 API 和版本 API
- 模板 / CSS：展示策略徽章、只读原因、版本源状态

## Task 1: 建立 Node / Python 策略层与规则配置

**Files:**
- Create: `app/models/policies.py`
- Create: `app/services/asset_policies.py`
- Create: `config/rules/node-packages.yaml`
- Create: `config/rules/python-packages.yaml`
- Create: `tests/test_asset_policies.py`
- Modify: `app/models/assets.py`

- [ ] **Step 1: 先写策略层失败测试，锁定“Node 保护名单 + Python 白名单”的行为**

```python
# tests/test_asset_policies.py
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
```

- [ ] **Step 2: 运行测试，确认因为策略层文件缺失而失败**

Run: `pytest tests/test_asset_policies.py -v`

Expected: FAIL，报 `ModuleNotFoundError: No module named 'app.services.asset_policies'`。

- [ ] **Step 3: 新增策略模型和规则服务最小实现**

```python
# app/models/policies.py
from pydantic import BaseModel, ConfigDict, Field


class PackageRule(BaseModel):
    model_config = ConfigDict(extra='forbid')

    name: str
    managed_by: str
    protected: bool = False
    allowed_actions: list[str] = Field(default_factory=list)
    full_delete_paths: list[str] = Field(default_factory=list)
    blocked_reason: str | None = None


class PackageRuleSet(BaseModel):
    model_config = ConfigDict(extra='forbid')

    packages: list[PackageRule] = Field(default_factory=list)
```

```python
# app/models/assets.py
class AssetSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')

    object_id: str
    category: str
    name: str
    status: str
    current_version: str | None = None
    latest_version: str | None = None
    supports_actions: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    containers: list[DockerContainerSnapshot] = Field(default_factory=list)
    primary_container_name: str | None = None
    actionable: bool = False
    blocked_reason: str | None = None
    managed_by: str | None = None
    policy_source: str | None = None
```

```python
# app/services/asset_policies.py
from pathlib import Path

import yaml

from app.models.assets import AssetSnapshot
from app.models.policies import PackageRuleSet

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
    def _load_rules(path: Path) -> dict[str, object]:
        payload = yaml.safe_load(path.read_text(encoding='utf-8')) if path.exists() else {'packages': []}
        rules = PackageRuleSet.model_validate(payload)
        return {rule.name: rule for rule in rules.packages}
```

- [ ] **Step 4: 写默认规则文件**

```yaml
# config/rules/node-packages.yaml
packages:
  - name: "@openai/codex"
    managed_by: agent_cli
    protected: true
    blocked_reason: 保留给 agent cli
  - name: "@anthropic-ai/claude-code"
    managed_by: agent_cli
    protected: true
    blocked_reason: 保留给 agent cli
  - name: "@google/gemini-cli"
    managed_by: agent_cli
    protected: true
    blocked_reason: 保留给 agent cli
  - name: "@jackwener/opencli"
    managed_by: agent_cli
    protected: true
    blocked_reason: 保留给 agent cli
  - name: "@qingchencloud/openclaw-zh"
    managed_by: agent_cli
    protected: true
    blocked_reason: 保留给 agent cli
```

```yaml
# config/rules/python-packages.yaml
packages:
  - name: openai
    managed_by: python
    allowed_actions: [update_latest, deploy_version, delete, full_delete]
  - name: fastapi
    managed_by: python
    allowed_actions: [update_latest, deploy_version, delete, full_delete]
  - name: uvicorn
    managed_by: python
    allowed_actions: [update_latest, deploy_version, delete, full_delete]
  - name: playwright
    managed_by: python
    allowed_actions: [update_latest, deploy_version, delete, full_delete]
```

- [ ] **Step 5: 重新运行策略测试，确认通过**

Run: `pytest tests/test_asset_policies.py -v`

Expected: PASS。

- [ ] **Step 6: 提交策略层**

```bash
git add app/models/policies.py app/models/assets.py app/services/asset_policies.py config/rules/node-packages.yaml config/rules/python-packages.yaml tests/test_asset_policies.py
git commit -m "feat: add package asset policy rules"
```

## Task 2: 把策略层接入 Node / Python 资产构建与页面展示

**Files:**
- Modify: `app/main.py`
- Modify: `app/services/assets.py`
- Modify: `app/templates/category.html`
- Modify: `app/templates/asset_detail.html`
- Modify: `app/static/app.css`
- Modify: `tests/test_readonly_scanners.py`
- Modify: `tests/test_overview_routes.py`

- [ ] **Step 1: 先写失败测试，锁定“展示但只读”和“展示可操作”的 UI 行为**

```python
# tests/test_readonly_scanners.py
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.scanners.node_scanner import parse_npm_package
from app.scanners.python_scanner import parse_pip_package


def test_node_and_python_pages_show_policy_badges_and_block_reasons(tmp_path: Path) -> None:
    categories = tmp_path / 'categories'
    categories.mkdir(parents=True, exist_ok=True)
    (categories / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (categories / 'python.yaml').write_text('id: python\nlabel: Python\norder: 40\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: "@openai/codex"\n    managed_by: agent_cli\n    protected: true\n    blocked_reason: 保留给 agent cli\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text(
        'packages:\n  - name: fastapi\n    managed_by: python\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )

    client = TestClient(
        create_app(
            config_root=tmp_path,
            node_scanner=lambda: [parse_npm_package('@openai/codex@0.128.0')],
            python_scanner=lambda: [parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})],
        )
    )
    client.cookies.set(COOKIE_NAME, issue_session_token())

    node_page = client.get('/categories/node')
    assert '保留给 agent cli' in node_page.text
    assert '只读' in node_page.text

    python_page = client.get('/categories/python')
    assert '可操作' in python_page.text
    assert 'fastapi' in python_page.text
```

```python
# tests/test_overview_routes.py
from app.scanners.node_scanner import parse_npm_package


def test_node_detail_hides_action_buttons_for_protected_package(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'node.yaml', 'id: node\nlabel: Node\norder: 30\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: "@openai/codex"\n    managed_by: agent_cli\n    protected: true\n    blocked_reason: 保留给 agent cli\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')

    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [parse_npm_package('@openai/codex@0.128.0')]))
    _login(client)

    response = client.get('/assets/bm9kZTpAb3BlbmFpL2NvZGV4')

    assert response.status_code == 200
    assert '保留给 agent cli' in response.text
    assert 'hx-post="/api/assets/' not in response.text
```

- [ ] **Step 2: 运行测试，确认因为 `AssetService` 仍把 Node / Python 当纯只读而失败**

Run: `pytest tests/test_readonly_scanners.py tests/test_overview_routes.py::test_node_detail_hides_action_buttons_for_protected_package -v`

Expected: FAIL，页面里没有策略徽章、没有 `blocked_reason`，或动作按钮渲染条件不正确。

- [ ] **Step 3: 在资产服务中接入策略层**

```python
# app/services/assets.py
from pathlib import Path

from app.services.asset_policies import AssetPolicyService

class AssetService:
    def __init__(
        self,
        registry_service: RegistryService,
        *,
        docker_scanner: DockerScanner | None = None,
        systemd_scanner: SystemdScanner | None = None,
        node_scanner: ReadonlyScanner | None = None,
        python_scanner: ReadonlyScanner | None = None,
        host_process_scanner: ReadonlyScanner | None = None,
        system_infra_scanner: ReadonlyScanner | None = None,
        config_root: Path | str = Path('config'),
    ) -> None:
        self._registry_service = registry_service
        self._docker_scanner = docker_scanner or scan_docker_containers
        self._systemd_scanner = systemd_scanner or scan_systemd_units
        self._node_scanner = node_scanner or scan_node_packages
        self._python_scanner = python_scanner or scan_python_packages
        self._host_process_scanner = host_process_scanner or scan_host_processes
        self._system_infra_scanner = system_infra_scanner or scan_system_infrastructure
        self._policy_service = AssetPolicyService(Path(config_root))

    def _build_assets_for_category(self, snapshot: RegistrySnapshot, category_id: str) -> list[AssetSnapshot]:
        if category_id == 'docker':
            return build_docker_asset_snapshots(snapshot, self._scan_docker_containers())
        if category_id == 'systemd':
            scanned_units, scan_failed = self._scan_systemd_units()
            return build_systemd_asset_snapshots(snapshot, scanned_units, scan_failed=scan_failed)
        if category_id == 'node':
            return [self._policy_service.apply(asset) for asset in self._scan_readonly_assets(self._node_scanner, 'node')]
        if category_id == 'python':
            return [self._policy_service.apply(asset) for asset in self._scan_readonly_assets(self._python_scanner, 'python')]
        if category_id == 'host':
            return self._scan_readonly_assets(self._host_process_scanner, 'host')
        if category_id == 'system':
            return self._scan_readonly_assets(self._system_infra_scanner, 'system')
        if category_id in {'agent_cli', 'agent'}:
            return []
        return []
```

```python
# app/main.py
asset_service = AssetService(
    registry_service,
    config_root=Path(config_root),
    docker_scanner=docker_scanner,
    systemd_scanner=systemd_scanner,
    node_scanner=node_scanner,
    python_scanner=python_scanner,
    host_process_scanner=host_process_scanner,
    system_infra_scanner=system_infra_scanner,
)
```

- [ ] **Step 4: 更新模板和样式，展示策略状态**

```html
<!-- app/templates/category.html -->
{% elif asset.category in ['node', 'python'] %}
<div class="asset-meta">版本：{{ asset.current_version or 'unknown' }}</div>
<div class="asset-meta">来源：{{ asset.metadata.package_manager }}</div>
<div class="policy-row">
  <span class="policy-pill {{ 'policy-pill-ok' if asset.actionable else 'policy-pill-blocked' }}">{{ '可操作' if asset.actionable else '只读' }}</span>
  {% if asset.managed_by %}<span class="policy-pill">{{ asset.managed_by }}</span>{% endif %}
  {% if asset.blocked_reason %}<span class="policy-pill policy-pill-blocked">{{ asset.blocked_reason }}</span>{% endif %}
</div>
{% endif %}
```

```html
<!-- app/templates/asset_detail.html -->
{% elif asset.category in ['node', 'python'] %}
<p>当前版本：{{ asset.current_version or 'unknown' }}</p>
<p>来源：{{ asset.metadata.package_manager }}</p>
<p>是否可操作：{{ '是' if asset.actionable else '否' }}</p>
<p>管理归属：{{ asset.managed_by or 'none' }}</p>
<p>阻止原因：{{ asset.blocked_reason or 'none' }}</p>
{% endif %}
```

```css
/* app/static/app.css */
.policy-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 0.5rem;
}

.policy-pill {
  border-radius: 999px;
  padding: 0.15rem 0.6rem;
  font-size: 0.8rem;
  background: #273449;
  color: #d5e2f3;
}

.policy-pill-ok {
  background: #1f5133;
  color: #d7f7e5;
}

.policy-pill-blocked {
  background: #5a2d2d;
  color: #ffd8d8;
}
```

- [ ] **Step 5: 重新运行页面测试，确认策略展示通过**

Run: `pytest tests/test_readonly_scanners.py tests/test_overview_routes.py::test_node_detail_hides_action_buttons_for_protected_package -v`

Expected: PASS。

- [ ] **Step 6: 提交资产策略接线**

```bash
git add app/main.py app/services/assets.py app/templates/category.html app/templates/asset_detail.html app/static/app.css tests/test_readonly_scanners.py tests/test_overview_routes.py
git commit -m "feat: apply package policies to node python assets"
```

## Task 3: 增加 npm / PyPI 版本源服务

**Files:**
- Create: `app/services/package_versions.py`
- Create: `tests/test_package_versions.py`
- Modify: `app/models/assets.py`
- Modify: `app/api/assets.py`

- [ ] **Step 1: 先写失败测试，锁定版本源解析与失败状态包装**

```python
# tests/test_package_versions.py
from app.services.package_versions import PackageVersionService


def test_npm_versions_parser_handles_json_array() -> None:
    service = PackageVersionService()

    def fake_runner(command: list[str]) -> str:
        if command[-1] == 'version':
            return '1.7.6\n'
        return '["1.7.4", "1.7.5", "1.7.6"]\n'

    info = service.get_node_version_info('@jackwener/opencli', runner=fake_runner)

    assert info.latest_version == '1.7.6'
    assert info.versions == ['1.7.4', '1.7.5', '1.7.6']
    assert info.source_status == 'ok'


def test_pypi_versions_wraps_failures() -> None:
    service = PackageVersionService()

    def failing_fetcher(package_name: str):
        raise RuntimeError('upstream unavailable')

    info = service.get_python_version_info('fastapi', fetcher=failing_fetcher)

    assert info.latest_version is None
    assert info.versions == []
    assert info.source_status == 'error'
    assert 'upstream unavailable' in (info.error or '')
```

- [ ] **Step 2: 运行测试，确认因为版本服务不存在而失败**

Run: `pytest tests/test_package_versions.py -v`

Expected: FAIL，报 `ModuleNotFoundError: No module named 'app.services.package_versions'`。

- [ ] **Step 3: 实现版本源服务和返回模型**

```python
# app/models/assets.py
class PackageVersionInfo(BaseModel):
    model_config = ConfigDict(extra='forbid')

    current_version: str | None = None
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = 'ok'
    error: str | None = None
```

```python
# app/services/package_versions.py
import json
import subprocess

import httpx

from app.models.assets import PackageVersionInfo


class PackageVersionService:
    def get_node_version_info(self, package_name: str, *, runner=None) -> PackageVersionInfo:
        run = runner or self._run_npm
        try:
            latest = run(['npm', 'view', package_name, 'version']).strip()
            raw_versions = run(['npm', 'view', package_name, 'versions', '--json'])
            payload = json.loads(raw_versions)
            versions = payload if isinstance(payload, list) else [str(payload)]
            return PackageVersionInfo(latest_version=latest or None, versions=[str(item) for item in versions], source_status='ok')
        except Exception as exc:
            return PackageVersionInfo(source_status='error', error=str(exc))

    def get_python_version_info(self, package_name: str, *, fetcher=None) -> PackageVersionInfo:
        get_json = fetcher or self._fetch_pypi_json
        try:
            payload = get_json(package_name)
            releases = payload.get('releases', {})
            versions = sorted(releases.keys())
            latest = payload.get('info', {}).get('version')
            return PackageVersionInfo(latest_version=latest, versions=versions, source_status='ok')
        except Exception as exc:
            return PackageVersionInfo(source_status='error', error=str(exc))

    @staticmethod
    def _run_npm(command: list[str]) -> str:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
        return completed.stdout

    @staticmethod
    def _fetch_pypi_json(package_name: str) -> dict:
        response = httpx.get(f'https://pypi.org/pypi/{package_name}/json', timeout=10.0)
        response.raise_for_status()
        return response.json()
```

- [ ] **Step 4: 扩展版本 API 响应结构**

```python
# app/api/assets.py
class AssetVersionsResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')

    object_id: str
    current_version: str | None = None
    latest_version: str | None = None
    versions: list[str] = Field(default_factory=list)
    source_status: str = 'ok'
    error: str | None = None
```

- [ ] **Step 5: 重新运行版本服务测试**

Run: `pytest tests/test_package_versions.py -v`

Expected: PASS。

- [ ] **Step 6: 提交版本源服务**

```bash
git add app/models/assets.py app/services/package_versions.py app/api/assets.py tests/test_package_versions.py
git commit -m "feat: add package version source service"
```

## Task 4: 实现 NodeAdapter 并接入动作 API

**Files:**
- Create: `app/adapters/node_adapter.py`
- Create: `tests/test_node_adapter.py`
- Create: `tests/test_package_asset_routes.py`
- Modify: `app/api/assets.py`
- Modify: `app/templates/asset_detail.html`

- [ ] **Step 1: 先写失败测试，锁定 Node 四控动作与 API 行为**

```python
# tests/test_node_adapter.py
from app.adapters.node_adapter import NodePackageAdapter


def test_node_adapter_builds_update_and_delete_plans() -> None:
    adapter = NodePackageAdapter(package_name='update', current_version='0.7.4', full_delete_paths=['/tmp/update-cache'])

    update_plan = adapter.plan_action('update_latest')
    delete_plan = adapter.plan_action('delete')
    full_delete_plan = adapter.plan_action('full_delete')

    assert update_plan.commands == [['npm', 'install', '-g', 'update@latest']]
    assert delete_plan.commands == [['npm', 'uninstall', '-g', 'update']]
    assert full_delete_plan.preview_paths == ['/tmp/update-cache']
    assert full_delete_plan.commands[-1] == ['rm', '-rf', '/tmp/update-cache']
```

```python
# tests/test_package_asset_routes.py
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
from app.scanners.node_scanner import parse_npm_package


def test_node_update_latest_route_enqueues_task_for_actionable_package(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')

    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [parse_npm_package('update@0.7.4')]))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post('/api/assets/bm9kZTp1cGRhdGU=/actions/update-latest')

    assert response.status_code == 202
    payload = response.json()
    assert payload['plan']['commands'] == [['npm', 'install', '-g', 'update@latest']]
```

- [ ] **Step 2: 运行测试，确认因为 NodeAdapter 和路由接线缺失而失败**

Run: `pytest tests/test_node_adapter.py tests/test_package_asset_routes.py::test_node_update_latest_route_enqueues_task_for_actionable_package -v`

Expected: FAIL，报 `ModuleNotFoundError` 或 `unsupported object type`。

- [ ] **Step 3: 实现 NodeAdapter**

```python
# app/adapters/node_adapter.py
from typing import Literal

from app.adapters.base import ActionPlan
from app.models.assets import PackageVersionInfo
from app.services.package_versions import PackageVersionService

NodeAction = Literal['update_latest', 'deploy_version', 'delete', 'full_delete']


class NodePackageAdapter:
    def __init__(self, *, package_name: str, current_version: str | None, full_delete_paths: list[str], version_service: PackageVersionService | None = None) -> None:
        self.package_name = package_name
        self.current_version = current_version
        self.full_delete_paths = full_delete_paths
        self._version_service = version_service or PackageVersionService()

    def plan_action(self, action: NodeAction, version: str | None = None) -> ActionPlan:
        if action == 'update_latest':
            return ActionPlan(commands=[['npm', 'install', '-g', f'{self.package_name}@latest']])
        if action == 'deploy_version':
            if not version:
                raise ValueError('deploy_version requires a target version')
            return ActionPlan(commands=[['npm', 'install', '-g', f'{self.package_name}@{version}']], preview_objects=[f'{self.package_name}@{version}'])
        if action == 'delete':
            return ActionPlan(commands=[['npm', 'uninstall', '-g', self.package_name]])
        if action == 'full_delete':
            commands = [['npm', 'uninstall', '-g', self.package_name]]
            for path in self.full_delete_paths:
                commands.append(['rm', '-rf', path])
            return ActionPlan(commands=commands, preview_objects=[self.package_name], preview_paths=self.full_delete_paths)
        raise ValueError(f'unsupported node action: {action}')

    def get_version_info(self) -> PackageVersionInfo:
        return self._version_service.get_node_version_info(self.package_name)

    def list_available_versions(self) -> list[str]:
        return self.get_version_info().versions
```

- [ ] **Step 4: 扩展 API，让 Node 资产不依赖 registry object 也能执行动作**

```python
# app/api/assets.py
from app.adapters.node_adapter import NodePackageAdapter
from app.adapters.python_adapter import PythonPackageAdapter

def _build_adapter(request: Request, object_id: str, asset: AssetSnapshot) -> ActionAdapter:
    if asset.category == 'node':
        _ensure_asset_actionable(asset)
        return NodePackageAdapter(
            package_name=asset.name,
            current_version=asset.current_version,
            full_delete_paths=list(asset.metadata.get('full_delete_paths', [])),
        )
    if asset.category == 'python':
        _ensure_asset_actionable(asset)
        return PythonPackageAdapter(
            package_name=asset.name,
            current_version=asset.current_version,
            full_delete_paths=list(asset.metadata.get('full_delete_paths', [])),
        )
    obj = _get_registry_object(request, object_id)
    if obj.type == 'docker_compose':
        return _build_docker_adapter(obj, asset)
    if obj.type == 'systemd_unit':
        return SystemdUnitAdapter(unit_name=obj.config['unit_name'], working_dir=obj.config['working_dir'])
    raise HTTPException(status_code=400, detail=f'unsupported object type: {obj.type}')


def _ensure_asset_actionable(asset: AssetSnapshot) -> None:
    if asset.actionable:
        return
    raise HTTPException(status_code=409, detail=asset.blocked_reason or 'asset is read only')
```

- [ ] **Step 5: 更新详情页，只有 `actionable` 时才显示按钮**

```html
<!-- app/templates/asset_detail.html -->
{% if asset.supports_actions and asset.actionable %}
<section class="panel action-panel">
  <h3>操作</h3>
  <div class="button-row">
    {% if 'update_latest' in asset.supports_actions %}
    <button hx-post="/api/assets/{{ asset.object_id }}/actions/update-latest" hx-target="#task-flash" hx-swap="innerHTML">更新最新版</button>
    {% endif %}
    {% if 'delete' in asset.supports_actions %}
    <button hx-post="/api/assets/{{ asset.object_id }}/actions/delete" hx-target="#task-flash" hx-swap="innerHTML">删除</button>
    {% endif %}
    {% if 'full_delete' in asset.supports_actions %}
    <button hx-post="/api/assets/{{ asset.object_id }}/actions/full-delete" hx-target="#task-flash" hx-swap="innerHTML">完全删除</button>
    {% endif %}
  </div>
  {% if 'deploy_version' in asset.supports_actions and available_versions %}
  <form class="inline-form" hx-post="/api/assets/{{ asset.object_id }}/actions/deploy-version" hx-target="#task-flash" hx-swap="innerHTML" method="post">
    <label>指定版本
      <select name="version">
        {% for version in available_versions %}
        <option value="{{ version }}">{{ version }}</option>
        {% endfor %}
      </select>
    </label>
    <button type="submit">部署版本</button>
  </form>
  {% endif %}
</section>
{% elif asset.category in ['node', 'python'] and not asset.actionable %}
<section class="panel action-panel">
  <h3>操作</h3>
  <p>当前对象只读：{{ asset.blocked_reason or '策略禁止' }}</p>
</section>
{% endif %}
```

- [ ] **Step 6: 重新运行 Node 适配器与路由测试**

Run: `pytest tests/test_node_adapter.py tests/test_package_asset_routes.py::test_node_update_latest_route_enqueues_task_for_actionable_package -v`

Expected: PASS。

- [ ] **Step 7: 提交 Node 动作能力**

```bash
git add app/adapters/node_adapter.py app/api/assets.py app/templates/asset_detail.html tests/test_node_adapter.py tests/test_package_asset_routes.py
git commit -m "feat: add node package asset actions"
```

## Task 5: 实现 PythonAdapter、版本接口与页面版本源状态

**Files:**
- Create: `app/adapters/python_adapter.py`
- Create: `tests/test_python_adapter.py`
- Modify: `app/api/assets.py`
- Modify: `app/api/overview.py`
- Modify: `app/templates/asset_detail.html`
- Modify: `tests/test_package_asset_routes.py`

- [ ] **Step 1: 先写失败测试，锁定 Python 四控动作与版本接口**

```python
# tests/test_python_adapter.py
from app.adapters.python_adapter import PythonPackageAdapter


def test_python_adapter_builds_deploy_and_full_delete_plans() -> None:
    adapter = PythonPackageAdapter(package_name='fastapi', current_version='0.115.0', full_delete_paths=['/tmp/fastapi-cache'])

    deploy_plan = adapter.plan_action('deploy_version', version='0.116.0')
    delete_plan = adapter.plan_action('delete')
    full_delete_plan = adapter.plan_action('full_delete')

    assert deploy_plan.commands == [['python3', '-m', 'pip', 'install', 'fastapi==0.116.0']]
    assert delete_plan.commands == [['python3', '-m', 'pip', 'uninstall', '-y', 'fastapi']]
    assert full_delete_plan.preview_paths == ['/tmp/fastapi-cache']
```

```python
# tests/test_package_asset_routes.py
from app.models.assets import PackageVersionInfo
from app.scanners.python_scanner import parse_pip_package


def test_python_versions_route_returns_source_status(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'python.yaml').write_text('id: python\nlabel: Python\norder: 40\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text(
        'packages:\n  - name: fastapi\n    managed_by: python\n    allowed_actions: [update_latest, deploy_version, delete, full_delete]\n',
        encoding='utf-8',
    )

    client = TestClient(create_app(config_root=tmp_path, python_scanner=lambda: [parse_pip_package({'name': 'fastapi', 'version': '0.115.0'})]))
    client.cookies.set(COOKIE_NAME, issue_session_token())

    from app.api import assets as assets_api
    monkeypatch.setattr(
        assets_api.PythonPackageAdapter,
        'get_version_info',
        lambda self: PackageVersionInfo(
            current_version='0.115.0',
            latest_version='0.116.0',
            versions=['0.115.0', '0.116.0'],
            source_status='ok',
        ),
    )

    response = client.get('/api/assets/cHl0aG9uOmZhc3RhcGk=/versions')

    assert response.status_code == 200
    payload = response.json()
    assert payload['latest_version'] == '0.116.0'
    assert payload['source_status'] == 'ok'
```

- [ ] **Step 2: 运行测试，确认因为 PythonAdapter 还不存在而失败**

Run: `pytest tests/test_python_adapter.py tests/test_package_asset_routes.py::test_python_versions_route_returns_source_status -v`

Expected: FAIL，报 `ModuleNotFoundError: No module named 'app.adapters.python_adapter'`。

- [ ] **Step 3: 实现 PythonAdapter**

```python
# app/adapters/python_adapter.py
from typing import Literal

from app.adapters.base import ActionPlan
from app.models.assets import PackageVersionInfo
from app.services.package_versions import PackageVersionService

PythonAction = Literal['update_latest', 'deploy_version', 'delete', 'full_delete']


class PythonPackageAdapter:
    def __init__(self, *, package_name: str, current_version: str | None, full_delete_paths: list[str], version_service: PackageVersionService | None = None) -> None:
        self.package_name = package_name
        self.current_version = current_version
        self.full_delete_paths = full_delete_paths
        self._version_service = version_service or PackageVersionService()

    def plan_action(self, action: PythonAction, version: str | None = None) -> ActionPlan:
        if action == 'update_latest':
            return ActionPlan(commands=[['python3', '-m', 'pip', 'install', '-U', self.package_name]])
        if action == 'deploy_version':
            if not version:
                raise ValueError('deploy_version requires a target version')
            return ActionPlan(commands=[['python3', '-m', 'pip', 'install', f'{self.package_name}=={version}']], preview_objects=[f'{self.package_name}=={version}'])
        if action == 'delete':
            return ActionPlan(commands=[['python3', '-m', 'pip', 'uninstall', '-y', self.package_name]])
        if action == 'full_delete':
            commands = [['python3', '-m', 'pip', 'uninstall', '-y', self.package_name]]
            for path in self.full_delete_paths:
                commands.append(['rm', '-rf', path])
            return ActionPlan(commands=commands, preview_objects=[self.package_name], preview_paths=self.full_delete_paths)
        raise ValueError(f'unsupported python action: {action}')

    def get_version_info(self) -> PackageVersionInfo:
        info = self._version_service.get_python_version_info(self.package_name)
        return info.model_copy(update={'current_version': self.current_version})

    def list_available_versions(self) -> list[str]:
        return self.get_version_info().versions
```

- [ ] **Step 4: 让 `/versions` 路由和详情页返回最新版本和源状态**

```python
# app/api/assets.py
from app.models.assets import PackageVersionInfo

@router.get('/{object_id}/versions', response_model=AssetVersionsResponse)
def get_asset_versions(object_id: str, request: Request) -> AssetVersionsResponse:
    asset = _get_asset(request, object_id)
    adapter = _build_adapter(request, object_id, asset)
    version_info = adapter.get_version_info() if hasattr(adapter, 'get_version_info') else PackageVersionInfo(current_version=asset.current_version, versions=adapter.list_available_versions())
    return AssetVersionsResponse(
        object_id=object_id,
        current_version=asset.current_version,
        latest_version=version_info.latest_version,
        versions=version_info.versions,
        source_status=version_info.source_status,
        error=version_info.error,
    )


def get_page_asset_version_info(request: Request, object_id: str, asset: AssetSnapshot) -> PackageVersionInfo | None:
    try:
        adapter = _build_adapter(request, object_id, asset)
    except HTTPException:
        return None
    if hasattr(adapter, 'get_version_info'):
        return adapter.get_version_info()
    return None
```

```python
# app/api/overview.py
from app.api.assets import get_page_asset, get_page_asset_version_info, get_page_asset_versions

@router.get('/assets/{object_id}', response_class=HTMLResponse, name='asset_detail')
def asset_detail_page(object_id: str, request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    asset = get_page_asset(request, object_id)
    available_versions = get_page_asset_versions(request, object_id, asset)
    version_info = get_page_asset_version_info(request, object_id, asset)
    context = build_page_context(
        request,
        title=asset.name,
        active_page=asset.category,
        asset=asset,
        available_versions=available_versions,
        version_info=version_info,
    )
    return TEMPLATES.TemplateResponse(request, 'asset_detail.html', context)
```

```html
<!-- app/templates/asset_detail.html -->
{% elif asset.category in ['node', 'python'] %}
<p>当前版本：{{ asset.current_version or 'unknown' }}</p>
<p>版本源状态：{{ version_info.source_status if version_info else 'unknown' }}</p>
<p>最新版本：{{ version_info.latest_version if version_info and version_info.latest_version else 'unknown' }}</p>
{% if version_info and version_info.error %}
<p>版本源错误：{{ version_info.error }}</p>
{% endif %}
{% endif %}
```

- [ ] **Step 5: 重新运行 Python 适配器与版本接口测试**

Run: `pytest tests/test_python_adapter.py tests/test_package_asset_routes.py::test_python_versions_route_returns_source_status -v`

Expected: PASS。

- [ ] **Step 6: 提交 Python 动作能力**

```bash
git add app/adapters/python_adapter.py app/api/assets.py app/api/overview.py app/templates/asset_detail.html tests/test_python_adapter.py tests/test_package_asset_routes.py
git commit -m "feat: add python package asset actions"
```

## Task 6: 收口集成测试与文档

**Files:**
- Modify: `tests/test_package_asset_routes.py`
- Modify: `README.md`
- Modify: `docs/operations.md`

- [ ] **Step 1: 补集成测试，锁定“受保护包返回 409”和“白名单外 Python 不显示动作”**

```python
# tests/test_package_asset_routes.py
from app.scanners.node_scanner import parse_npm_package
from app.scanners.python_scanner import parse_pip_package


def test_protected_node_package_returns_conflict_on_action(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'node.yaml').write_text('id: node\nlabel: Node\norder: 30\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text(
        'packages:\n  - name: "@openai/codex"\n    managed_by: agent_cli\n    protected: true\n    blocked_reason: 保留给 agent cli\n',
        encoding='utf-8',
    )
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    client = TestClient(create_app(config_root=tmp_path, node_scanner=lambda: [parse_npm_package('@openai/codex@0.128.0')]))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    response = client.post('/api/assets/bm9kZTpAb3BlbmFpL2NvZGV4/actions/delete')
    assert response.status_code == 409
    assert response.json()['detail'] == '保留给 agent cli'


def test_non_whitelisted_python_package_detail_is_read_only(tmp_path: Path) -> None:
    (tmp_path / 'categories').mkdir(parents=True, exist_ok=True)
    (tmp_path / 'categories' / 'python.yaml').write_text('id: python\nlabel: Python\norder: 40\nenabled: true\n', encoding='utf-8')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    rules = tmp_path / 'rules'
    rules.mkdir(parents=True, exist_ok=True)
    (rules / 'node-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    (rules / 'python-packages.yaml').write_text('packages: []\n', encoding='utf-8')
    client = TestClient(create_app(config_root=tmp_path, python_scanner=lambda: [parse_pip_package({'name': 'requests', 'version': '2.32.0'})]))
    client.cookies.set(COOKIE_NAME, issue_session_token())
    response = client.get('/assets/cHl0aG9uOnJlcXVlc3Rz')
    assert response.status_code == 200
    assert '白名单外' in response.text
    assert 'hx-post="/api/assets/' not in response.text
```

- [ ] **Step 2: 更新 README 和运维说明**

```markdown
# README.md
- Node 分类支持 npm 全局包四控
- Python 分类支持 Miniconda base 白名单包四控
- agent CLI 相关 npm 包当前在 Node 分类中只展示，不执行
```

```markdown
# docs/operations.md
### 3.3 Node
- 默认管理 `npm list -g` 扫到的普通全局包
- `@openai/codex`、`@anthropic-ai/claude-code`、`@google/gemini-cli`、`@jackwener/opencli`、`@qingchencloud/openclaw-zh` 当前受保护

### 3.4 Python
- 当前只管理 `python3 -m pip list` 对应的 Miniconda base
- 仅 `openai / fastapi / uvicorn / playwright` 开放执行动作
```

- [ ] **Step 3: 跑完整验证**

Run:

```bash
pytest -q
ruff check app tests
bash -n scripts/check_sudo_rules.sh scripts/install_systemd_service.sh
```

Expected: 全部通过。

- [ ] **Step 4: 提交收尾**

```bash
git add tests/test_package_asset_routes.py README.md docs/operations.md
git commit -m "docs: describe node python package management"
```

## Self-Review

- Spec coverage：Task 1 覆盖规则与白名单；Task 2 覆盖策略展示；Task 3 覆盖 npm / PyPI 版本源；Task 4 和 Task 5 覆盖 Node / Python 四控动作；Task 6 覆盖失败语义和文档收口。
- Placeholder scan：计划中没有 `TODO`、`TBD`、`implement later` 之类的占位词。
- Type consistency：`AssetSnapshot.actionable`、`blocked_reason`、`managed_by`、`PackageVersionInfo`、`NodePackageAdapter`、`PythonPackageAdapter`、`AssetPolicyService` 在所有任务中的命名保持一致。

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-07-wsl-ops-panel-node-python-management.md`. Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
