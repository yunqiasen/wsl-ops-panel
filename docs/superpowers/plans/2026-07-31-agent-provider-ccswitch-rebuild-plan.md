# Agent Provider CC Switch Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 将 Agent Provider 重构为数据库原生配置事实源，并按客户端模式完成导入、编辑、启用、复制和删除。

**Architecture:** 新增 `agent_provider_adapters.py` 负责客户端原生配置与统一摘要/Router Profile 的双向转换；`AgentProviderStore` 只负责数据库、脱敏和迁移。前端根据服务端 `summary` 和客户端类型渲染专属字段，启用动作由后端按独占、累加和 Router 接管三种状态分派。

**Tech Stack:** FastAPI、Pydantic、SQLite、Jinja2、原生 JavaScript/CSS、pytest、Node test runner。

---

### Task 1: Provider 数据库元数据和旧记录迁移

**Files:**
- Modify: `app/services/state_store.py`
- Modify: `app/services/agent_providers.py`
- Test: `tests/test_agent_database_resources.py`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: 写失败测试**

新增断言：`agent_providers` 有 `meta_json`；Provider meta 可往返；启动旧数据库时把 `local-current` 重命名为 `default`；若已存在 `default` 则保留两条记录且不覆盖；Router 当前 Provider 引用同步改为 `default`。

- [x] **Step 2: 验证 RED**

Run: `.venv/bin/pytest -q tests/test_agent_database_resources.py tests/test_agent_workbench.py -k 'provider_meta or local_current or provider_migration'`
Expected: `meta_json` 和迁移方法不存在导致失败。

- [x] **Step 3: 最小实现**

在 `agent_providers` 增加 `meta_json TEXT NOT NULL DEFAULT '{}'`，扩展 `_provider_from_row()`、`list_agent_providers()`、`get_agent_provider()`、`upsert_agent_provider(meta=...)`。新增 `_migrate_agent_provider_local_current()`，事务内更新 Provider ID、Router Provider ID 和相关 Secret ref。

- [x] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_database_resources.py tests/test_agent_workbench.py -k 'provider_meta or local_current or provider_migration'`
Expected: 全部通过。

- [x] **Step 5: 提交**

```bash
git add app/services/state_store.py app/services/agent_providers.py tests/test_agent_database_resources.py tests/test_agent_workbench.py
git commit -m "feat: migrate provider metadata and legacy snapshots"
```

### Task 2: 客户端 Provider 适配器

**Files:**
- Create: `app/services/agent_provider_adapters.py`
- Modify: `app/services/agent_provider_profiles.py`
- Modify: `app/services/agent_providers.py`
- Test: `tests/test_agent_provider_adapters.py`
- Test: `tests/test_agent_provider_profiles.py`

- [x] **Step 1: 写失败测试**

覆盖七个客户端：

```python
summary = summarize_provider("codex", settings, meta)
assert summary["base_url"] == "https://relay.example/v1"
assert summary["model"] == "gpt-5.6"
assert summary["api_format"] == "openai_responses"
```

并断言 `build_native_settings()` 修改表单字段时保留未知原生字段；`build_runtime_profile()` 从原生配置和 meta 生成 Router Profile；`read_provider_records()` 对独占客户端生成 `default`，对 OpenCode/OpenClaw/Hermes 分拆原生 Provider。

- [x] **Step 2: 验证 RED**

Run: `.venv/bin/pytest -q tests/test_agent_provider_adapters.py tests/test_agent_provider_profiles.py`
Expected: 新模块和函数不存在。

- [x] **Step 3: 最小实现**

实现：

```python
EXCLUSIVE_PROVIDER_APPS = {"claude", "codex", "gemini", "grokbuild"}
ADDITIVE_PROVIDER_APPS = {"opencode", "openclaw", "hermes"}

def summarize_provider(app_id, settings, meta): ...
def build_native_settings(app_id, form, existing): ...
def build_provider_form(app_id, settings, meta): ...
def build_runtime_profile(app_id, settings, meta): ...
def read_provider_records(home, app_id, environ=None, overrides=None): ...
```

Claude 使用 `env`；Codex 解析 TOML 的 `model_provider/model_providers`；Gemini 使用 `env/config`；Grok 解析 `[models]` 与 `[model.*]`；OpenCode/OpenClaw/Hermes 按各自原生 Provider 容器解析。

- [x] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_provider_adapters.py tests/test_agent_provider_profiles.py`
Expected: 全部通过。

- [x] **Step 5: 提交**

```bash
git add app/services/agent_provider_adapters.py app/services/agent_provider_profiles.py app/services/agent_providers.py tests/test_agent_provider_adapters.py tests/test_agent_provider_profiles.py
git commit -m "feat: add native provider adapters"
```

### Task 3: Provider Store、导入和详情 API

**Files:**
- Modify: `app/services/agent_providers.py`
- Modify: `app/services/agent_workbench.py`
- Modify: `app/api/agent.py`
- Test: `tests/test_agent_workbench.py`
- Test: `tests/test_agent_resource_api.py`

- [x] **Step 1: 写失败测试**

断言：详情包含 `meta`、`summary`、`form` 和脱敏 `settings_config`；独占导入已有用户 Provider 时返回 0 且不覆盖；累加导入按原生 ID 新增；Hermes 只读来源带 `editable=false/read_only_reason`；列表不包含完整原生配置。

- [x] **Step 2: 验证 RED**

Run: `.venv/bin/pytest -q tests/test_agent_workbench.py tests/test_agent_resource_api.py -k 'provider and (summary or form or additive or import)'`
Expected: 新返回字段和导入语义缺失。

- [x] **Step 3: 最小实现**

`AgentProviderStore.import_from_home()` 调用适配器；`public_provider()` 增加摘要；详情 API 增加表单数据。`AgentProviderUpsertRequest` 增加 `form` 和 `meta`，后端通过 `build_native_settings()` 生成原生配置，不再要求前端构造 `routing`。

- [x] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_workbench.py tests/test_agent_resource_api.py -k 'provider and (summary or form or additive or import)'`
Expected: 全部通过。

- [x] **Step 5: 提交**

```bash
git add app/services/agent_providers.py app/services/agent_workbench.py app/api/agent.py tests/test_agent_workbench.py tests/test_agent_resource_api.py
git commit -m "feat: expose native provider details and imports"
```

### Task 4: 启用、复制和删除动作

**Files:**
- Modify: `app/services/agent_providers.py`
- Modify: `app/services/agent_router_config.py`
- Modify: `app/api/agent.py`
- Test: `tests/test_agent_workbench.py`
- Test: `tests/test_agent_router.py`

- [x] **Step 1: 写失败测试**

覆盖：独占直连启用写客户端并设当前；Router 接管态只更新 Router Provider；OpenCode/OpenClaw/Hermes 添加和移除不删除数据库；复制生成唯一 ID、复制描述/meta/settings 并追加末尾；当前独占 Provider 删除返回 409；只读 Hermes 删除返回 409。

- [x] **Step 2: 验证 RED**

Run: `.venv/bin/pytest -q tests/test_agent_workbench.py tests/test_agent_router.py -k 'provider and (activate or duplicate or remove or delete)'`
Expected: activate/duplicate API 404 或语义不符。

- [x] **Step 3: 最小实现**

新增：

```text
POST /api/agent/providers/{app_id}/{provider_id}/activate
POST /api/agent/providers/{app_id}/{provider_id}/duplicate
POST /api/agent/providers/{app_id}/{provider_id}/remove-live
```

`activate` 根据接管状态和 `ADDITIVE_PROVIDER_APPS` 分派；复制默认 ID 为 `<id>-copy`，冲突时递增；删除先检查 current、read-only 和 live 投影。

- [x] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_workbench.py tests/test_agent_router.py -k 'provider and (activate or duplicate or remove or delete)'`
Expected: 全部通过。

- [x] **Step 5: 提交**

```bash
git add app/services/agent_providers.py app/services/agent_router_config.py app/api/agent.py tests/test_agent_workbench.py tests/test_agent_router.py
git commit -m "feat: add provider activation and duplication"
```

### Task 5: CC Switch 风格 Provider UI

**Files:**
- Modify: `app/templates/agent_category.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Test: `tests/frontend/agent-workbench.test.cjs`
- Test: `tests/test_agent_workbench.py`

- [x] **Step 1: 写失败测试**

断言卡片显示 `data-provider-base-url/model/format/auth-state`；存在启用、编辑、复制、检测、删除按钮；编辑器含七客户端专属字段容器；保存请求发送 `form/meta`；卡片主动作使用 activate/remove-live；不再使用 `settings.routing` 作为编辑回填源。

- [x] **Step 2: 验证 RED**

Run: `node --test tests/frontend/agent-workbench.test.cjs && .venv/bin/pytest -q tests/test_agent_workbench.py -k 'provider and page'`
Expected: 新 DOM、helper 和请求路径缺失。

- [x] **Step 3: 最小实现**

卡片改为三行紧凑布局；编辑器公共字段下增加客户端专属字段。`fillProvider()` 使用详情 API 的 `form`；`providerPayload()` 发送表单对象；保存只入库。主按钮调用 activate/remove-live，复制调用 duplicate；只读项禁用编辑和删除并显示原因。

- [x] **Step 4: 响应式与无障碍**

1600px 使用紧凑横向信息布局；390px 变为单列但按钮保持 40–44px 触控尺寸。状态不用仅靠颜色表达；隐藏字段使用 `hidden`；按钮提供 `aria-label/title`；URL 和模型使用省略并保留完整 title。

- [x] **Step 5: 验证 GREEN**

Run: `node --test tests/frontend/agent-workbench.test.cjs && .venv/bin/pytest -q tests/test_agent_workbench.py -k 'provider and page'`
Expected: 全部通过。

- [x] **Step 6: 提交**

```bash
git add app/templates/agent_category.html app/static/app.js app/static/app.css tests/frontend/agent-workbench.test.cjs tests/test_agent_workbench.py
git commit -m "feat: rebuild provider workbench UI"
```

### Task 6: 全量验证、生产回灌和文档

**Files:**
- Modify: `docs/operations.md`
- Modify after patch-back: `/home/div/1_Project_dir/AI/wsl-ops-panel/agent-audit-20260713-130739.md`

- [x] **Step 1: 定向门禁**

Run: `.venv/bin/pytest -q tests/test_agent_provider_adapters.py tests/test_agent_provider_profiles.py tests/test_agent_workbench.py tests/test_agent_resource_api.py tests/test_agent_router.py && node --test tests/frontend/agent-workbench.test.cjs`
Expected: 全部通过。

- [x] **Step 2: 完整门禁**

Run: `.venv/bin/pytest -q && node --test tests/frontend/*.test.cjs && .venv/bin/ruff check app tests && .venv/bin/python -m compileall -q app tests && node --check app/static/app.js && git diff --check`
Expected: 所有命令退出 0。

- [x] **Step 3: 隔离 Home 浏览器验收**

准备七客户端 fixture，逐客户端验证导入、显示、编辑、复制、启用/添加、移除和删除；验证 Router 接管态热切换不覆盖 live；检查桌面 1600px、移动 390px、控制台、失败请求和 `>=400` 响应。

- [x] **Step 4: 生产回灌**

生成限定文件补丁并应用到 `/home/div/1_Project_dir/AI/wsl-ops-panel`，不覆盖原工作区其他修改。备份 `data/state.db`，启动迁移后验证 `integrity_check=ok`、`local-current` 已变为 `default`、Secret 和 Router 引用仍有效。

- [x] **Step 5: 生产运行态验收**

重启 `wsl-ops-panel.service` 与 `wsl-agent-router.service`；验证 `8328/healthz`、`7888/health`；真实页面验证 Claude/Codex 导入记录可见端点和可编辑字段。

- [x] **Step 6: 文档和审计**

更新 `docs/operations.md` 的 Provider 事实源、客户端模式、导入、启用和 Router 边界；把测试、数据库迁移和浏览器证据写入审计文件。

- [x] **Step 7: 提交**

```bash
git add docs/operations.md docs/superpowers/plans/2026-07-31-agent-provider-ccswitch-rebuild-plan.md
git commit -m "docs: document provider rebuild operations"
```
