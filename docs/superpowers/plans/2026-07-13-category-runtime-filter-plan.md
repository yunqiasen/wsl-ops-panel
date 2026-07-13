# Category Runtime Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在通用资产分类页增加可与文字搜索组合使用的“运行中/已关闭”筛选按钮。

**Architecture:** 服务端模板把现有资产状态标准化为 `running/stopped/unknown` 并写入卡片 data 属性；浏览器端用两个互斥按钮维护单一运行态筛选值，再与搜索词做 AND 组合。实现限定在现有模板、静态资源和路由测试，不修改扫描器、数据库或任务队列。

**Tech Stack:** FastAPI、Jinja2、原生 JavaScript、CSS、pytest、Node.js 内置 test runner

---

## 文件边界

- 修改 `app/templates/category.html`：渲染按钮和 `data-runtime-state`。
- 修改 `app/static/app.js`：提供纯筛选函数并扩展 `initAssetSearch()`。
- 修改 `app/static/app.css`：桌面、激活态和窄屏布局。
- 修改 `tests/test_overview_routes.py`：模板、状态映射和分类边界测试。
- 创建 `tests/frontend/category-runtime-filter.test.cjs`：组合筛选状态机测试。
- 修改 `docs/operations.md`：记录用户可见行为。

当前工作树已有大量未提交改动。实施时只编辑上述范围；不要 reset、checkout 或提交共享脏文件的整文件历史差异。

### Task 1: 建立失败测试

**Files:**
- Modify: `tests/test_overview_routes.py`
- Create: `tests/frontend/category-runtime-filter.test.cjs`

- [ ] **Step 1: 写模板失败测试**

在 `tests/test_overview_routes.py` 增加测试，构造一个运行中的 Docker 对象和一个无主容器的停止对象，并断言：

```python
def test_category_runtime_filter_renders_controls_and_normalized_states(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'docker.yaml', 'id: docker\nlabel: Docker\norder: 10\nenabled: true\n')
    _write_registry_file(tmp_path, 'objects', 'cpa.yaml', _docker_object_yaml())
    _write_registry_file(
        tmp_path,
        'objects',
        'stopped.yaml',
        'id: stopped\ncategory: docker\ntype: docker_compose\nname: Stopped App\nconfig:\n'
        '  project_dir: /srv/stopped\n  compose_file: docker-compose.yml\n'
        '  primary_container: stopped-app\n  compose_service: stopped-app\n',
    )
    client = TestClient(create_app(config_root=tmp_path, docker_scanner=lambda: parse_docker_ps_lines([_docker_ps_line()])))
    _login(client)

    response = client.get('/categories/docker')

    assert response.status_code == 200
    assert 'data-runtime-filter="running"' in response.text
    assert 'data-runtime-filter="stopped"' in response.text
    assert 'aria-pressed="false"' in response.text
    assert 'data-runtime-state="running"' in response.text
    assert 'data-runtime-state="stopped"' in response.text
```

再增加系统基础设施边界测试：

```python
def test_system_category_does_not_render_runtime_filter_controls(tmp_path: Path) -> None:
    _write_registry_file(tmp_path, 'categories', 'system.yaml', 'id: system\nlabel: 系统基础设施\norder: 90\nenabled: true\n')
    (tmp_path / 'objects').mkdir(parents=True, exist_ok=True)
    asset = AssetSnapshot(object_id='system__docker', category='system', name='Docker', status='available')
    client = TestClient(create_app(config_root=tmp_path, system_infra_scanner=lambda: [asset]))
    _login(client)

    response = client.get('/categories/system')

    assert response.status_code == 200
    assert 'data-runtime-filter=' not in response.text
    assert 'data-runtime-state="unknown"' in response.text
```

- [ ] **Step 2: 写 JavaScript 失败测试**

创建 `tests/frontend/category-runtime-filter.test.cjs`：

```javascript
const test = require('node:test');
const assert = require('node:assert/strict');
const { matchesAssetFilter, nextRuntimeFilter } = require('../../app/static/app.js');

test('文字和状态筛选使用 AND 组合', () => {
  assert.equal(matchesAssetFilter('CPA API', 'running', 'cpa', 'running'), true);
  assert.equal(matchesAssetFilter('CPA API', 'running', 'cpa', 'stopped'), false);
  assert.equal(matchesAssetFilter('CPA API', 'unknown', '', 'running'), false);
});

test('再次点击激活按钮恢复全部', () => {
  assert.equal(nextRuntimeFilter('', 'running'), 'running');
  assert.equal(nextRuntimeFilter('running', 'running'), '');
  assert.equal(nextRuntimeFilter('running', 'stopped'), 'stopped');
});
```

- [ ] **Step 3: 运行测试确认失败**

Run:

```bash
.venv/bin/pytest -q tests/test_overview_routes.py -k runtime_filter
node --test tests/frontend/category-runtime-filter.test.cjs
```

Expected: pytest 因缺少按钮/data 属性失败；Node 因未导出函数失败。

### Task 2: 实现模板与组合筛选

**Files:**
- Modify: `app/templates/category.html`
- Modify: `app/static/app.js`

- [ ] **Step 1: 在模板中增加按钮与状态属性**

在搜索面板中为 `docker/project/systemd/host` 渲染：

```jinja2
{% set runtime_filter_categories = ['docker', 'project', 'systemd', 'host'] %}
{% if selected_category and selected_category.id in runtime_filter_categories %}
<div class="category-runtime-filters" role="group" aria-label="运行状态筛选">
  <button class="runtime-filter-button runtime-filter-button--running" type="button" data-runtime-filter="running" aria-pressed="false">运行中</button>
  <button class="runtime-filter-button runtime-filter-button--stopped" type="button" data-runtime-filter="stopped" aria-pressed="false">已关闭</button>
</div>
{% endif %}
```

在每张卡片前计算状态并写入属性：

```jinja2
{% set asset_status = asset.status|lower %}
{% set runtime_state = 'running' if asset_status in ['running', 'active', 'listening'] or asset_status.startswith('up') else 'stopped' if asset_status in ['not running', 'inactive', 'failed', 'dead', 'stopped'] or asset_status.startswith('exited') else 'unknown' %}
<li class="asset-card" data-asset-card data-runtime-state="{{ runtime_state }}" ...>
```

- [ ] **Step 2: 增加纯函数并扩展 `initAssetSearch()`**

在 `app/static/app.js` 增加：

```javascript
function nextRuntimeFilter(current, requested) {
  return current === requested ? '' : requested;
}

function matchesAssetFilter(haystack, runtimeState, query, runtimeFilter) {
  const normalizedQuery = String(query || '').trim().toLowerCase();
  const matchesQuery = !normalizedQuery || String(haystack || '').toLowerCase().includes(normalizedQuery);
  const matchesRuntime = !runtimeFilter || runtimeState === runtimeFilter;
  return matchesQuery && matchesRuntime;
}
```

让 `initAssetSearch()` 读取 `[data-runtime-filter]`，维护 `activeRuntimeFilter`，每次点击同步 `aria-pressed` 和 `is-active`，再调用统一 `applyFilter()`。将底部 DOM 注册改为环境守卫，并为 Node 测试导出纯函数：

```javascript
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { matchesAssetFilter, nextRuntimeFilter };
}
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => {
    installHtmxFallback();
    initVersionPanels();
    initVersionBadges();
    initAssetSearch();
    initBulkSelection();
    initPackageInstallPanel();
    initConfigSyncPanel();
    initAgentWorkbench();
    initNotificationPanel();
    initNotificationDialog();
    initHostPortActions();
    initTerminalWorkbench();
  });
}
```

- [ ] **Step 3: 运行定向测试确认通过**

Run:

```bash
.venv/bin/pytest -q tests/test_overview_routes.py -k runtime_filter
node --test tests/frontend/category-runtime-filter.test.cjs
```

Expected: 所有定向测试通过。

### Task 3: 完成视觉、响应式与移动端布局

**Files:**
- Modify: `app/static/app.css`

- [ ] **Step 1: 增加桌面样式**

```css
.category-runtime-filters {
  display: inline-grid;
  grid-template-columns: repeat(2, minmax(88px, 1fr));
  gap: 8px;
}

.runtime-filter-button {
  min-height: 42px;
  padding: 8px 14px;
  border: 1px solid #d7e3f4;
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.9);
  color: #52637a;
  font: inherit;
  font-weight: 750;
  cursor: pointer;
}

.runtime-filter-button--running.is-active {
  border-color: #86efac;
  background: #ecfdf3;
  color: #087443;
}

.runtime-filter-button--stopped.is-active {
  border-color: #bfcee1;
  background: #eef3f8;
  color: #34445a;
}
```

- [ ] **Step 2: 增加窄屏样式**

在现有 `@media (max-width: 720px)` 中增加：

```css
.category-runtime-filters {
  width: 100%;
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.category-filter-meta {
  justify-content: center;
}
```

- [ ] **Step 3: 运行页面与 CSS 定向测试**

Run:

```bash
.venv/bin/pytest -q tests/test_overview_routes.py
node --test tests/frontend/category-runtime-filter.test.cjs
```

Expected: 全部通过。

### Task 4: 更新运维文档

**Files:**
- Modify: `docs/operations.md`

- [ ] **Step 1: 增加分类筛选说明**

在支持范围后增加：

```markdown
### 3.7 分类页搜索与运行状态筛选

- Docker、Project、systemd、宿主机进程分类支持“运行中/已关闭”筛选。
- 两个状态按钮互斥；再次点击当前按钮恢复全部。
- 文字搜索与状态筛选同时生效，右侧数量为最终可见资产数。
- 无法确认运行态的资产只在默认视图显示，不会被归入“已关闭”。
- 宿主机进程页只扫描当前监听项，不保留已关闭进程历史。
```

- [ ] **Step 2: 检查文档和差异格式**

Run:

```bash
git diff --check -- app/templates/category.html app/static/app.js app/static/app.css tests/test_overview_routes.py tests/frontend/category-runtime-filter.test.cjs docs/operations.md
```

Expected: 无输出，退出码 0。

### Task 5: 全量验证与部署

**Files:**
- Verify only

- [ ] **Step 1: 运行完整测试与静态检查**

Run:

```bash
.venv/bin/pytest -q
node --test tests/frontend/category-runtime-filter.test.cjs
.venv/bin/ruff check .
git diff --check
```

Expected: pytest、Node 测试、ruff 全部通过，diff check 无输出。

- [ ] **Step 2: 重启服务并验证 HTTP**

Run:

```bash
sudo systemctl restart wsl-ops-panel.service
systemctl is-active wsl-ops-panel.service
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8328/login
curl -sS -o /dev/null -w '%{http_code}\n' http://100.126.43.55:8328/login
```

Expected: 服务为 `active`，两个 URL 均返回 `200`。

- [ ] **Step 3: 浏览器验收**

在已登录的 Docker 分类页验证：

1. “运行中”只显示 `data-runtime-state=running` 卡片。
2. “已关闭”只显示 `data-runtime-state=stopped` 卡片。
3. 再次点击恢复全部。
4. 输入 `new api` 后叠加“已关闭”，只保留对应停止卡片。
5. 视口缩至 720px 以下，按钮等宽且搜索框不被挤压。
