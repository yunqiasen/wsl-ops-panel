# Agent Resource Create, Description, and Order Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 Provider、MCP、Skill、Prompt、Profile 补齐明确新增入口、可编辑描述和数据库持久化列表排序。

**Architecture:** 在现有 SQLite 资源表增加 `sort_index`，Prompt 增加 `description`；由 `PanelStateStore` 统一验证并事务更新顺序。前端使用统一资源排序接口，桌面拖拽与键盘/触屏移动按钮共用同一保存路径。

**Tech Stack:** FastAPI、Pydantic、SQLite、Jinja2、原生 JavaScript/CSS、pytest、Node test runner。

---

### Task 1: SQLite 描述与顺序

**Files:**
- Modify: `app/services/state_store.py`
- Modify: `app/services/agent_prompts.py`
- Test: `tests/test_agent_database_resources.py`

- [x] **Step 1: 写失败测试**

新增测试：Prompt `description` 往返；MCP/Skill/Prompt/Profile/Provider 新增时追加末尾；`reorder_agent_resources()` 重排后重启数据库仍保持；重复 ID、遗漏 ID 和 Provider 缺少 `client_id` 抛出 `ValueError`。

- [x] **Step 2: 验证 RED**

Run: `.venv/bin/pytest -q tests/test_agent_database_resources.py -k 'description or order or reorder'`
Expected: Prompt 方法签名、`sort_index` 字段和 `reorder_agent_resources` 缺失导致失败。

- [x] **Step 3: 最小实现**

为 `agent_mcp_servers`、`agent_skills`、`agent_prompts`、`agent_profiles` 增加 `sort_index`；为 Prompt 增加 `description`。查询按 `sort_index, name, id`；新记录使用同分类 `MAX(sort_index)+1`，更新保留位置。新增 `PanelStateStore.reorder_agent_resources(resource_type, resource_ids, client_id=None)`，在单事务内校验完整集合并写连续序号。

- [x] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_database_resources.py`
Expected: 全部通过。

- [x] **Step 5: 提交**

```bash
git add app/services/state_store.py app/services/agent_prompts.py tests/test_agent_database_resources.py
git commit -m "feat: persist agent resource descriptions and order"
```

### Task 2: 排序与描述 API

**Files:**
- Modify: `app/api/agent.py`
- Test: `tests/test_agent_resource_api.py`

- [x] **Step 1: 写失败测试**

新增 API 测试：Prompt 保存并回读描述；`PUT /api/agent/resources/order` 对 MCP、Skill、Prompt、Profile 和当前客户端 Provider 持久化顺序；重复/遗漏 ID 返回 `400/409`；Provider 缺少客户端返回 `400`。

- [x] **Step 2: 验证 RED**

Run: `.venv/bin/pytest -q tests/test_agent_resource_api.py -k 'description or order'`
Expected: 排序路由 404，Prompt 请求因额外 `description` 被拒绝。

- [x] **Step 3: 最小实现**

扩展 `AgentPromptUpsertRequest.description`。新增 `AgentResourceOrderRequest`：`resource_type` 为 provider/mcp/skill/prompt/profile，`resource_ids` 至少一项，`client_id` 可选。路由调用共享 `PanelStateStore`，把校验错误转换为明确 HTTP 响应，并返回保存后的 ID 顺序。

- [x] **Step 4: 验证 GREEN**

Run: `.venv/bin/pytest -q tests/test_agent_resource_api.py`
Expected: 全部通过。

- [x] **Step 5: 提交**

```bash
git add app/api/agent.py tests/test_agent_resource_api.py
git commit -m "feat: expose agent resource ordering api"
```

### Task 3: 新增、描述与拖拽 UI

**Files:**
- Modify: `app/templates/agent_category.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Modify: `app/services/agent_workbench.py`
- Test: `tests/frontend/agent-workbench.test.cjs`

- [x] **Step 1: 写失败测试**

断言五类标题都有新增按钮，五类编辑器都有描述字段；导出并测试 `moveAgentResourceId()` 的首尾和上下移动；断言 `buildAgentResourceOrderPayload()` 对 Provider 带 `client_id`；模板包含拖拽手柄、上移和下移控制。

- [x] **Step 2: 验证 RED**

Run: `node --test tests/frontend/agent-workbench.test.cjs`
Expected: 新按钮、描述字段和排序 helper 缺失导致失败。

- [x] **Step 3: 最小实现**

统一卡片渲染：显示描述、`draggable=true`、拖拽手柄、上移/下移按钮。新增按钮清空编辑器并解除 ID 只读；编辑时填充描述并锁定 ID。保存 MCP/Prompt/Provider 时发送描述。列表 drag/drop 或移动按钮后调用 `PUT /api/agent/resources/order`，失败则 `loadAgentLibrary()` 回滚。

- [x] **Step 4: 响应式与可访问性**

拖拽手柄使用可见焦点和 `cursor: grab`；拖动卡片降低透明度并高亮插入目标；390px 将移动按钮保持 44px 触控区域，列表不横向溢出；`prefers-reduced-motion` 下关闭位移动画。

- [x] **Step 5: 验证 GREEN**

Run: `node --test tests/frontend/agent-workbench.test.cjs`
Expected: 全部通过。

- [x] **Step 6: 提交**

```bash
git add app/templates/agent_category.html app/static/app.js app/static/app.css app/services/agent_workbench.py tests/frontend/agent-workbench.test.cjs
git commit -m "feat: add and reorder agent resource cards"
```

### Task 4: 运行态验收与文档

**Files:**
- Modify: `docs/operations.md`
- Modify after patch-back: `/home/div/1_Project_dir/AI/wsl-ops-panel/agent-audit-20260713-130739.md`

- [x] **Step 1: 定向门禁**

Run: `.venv/bin/pytest -q tests/test_agent_database_resources.py tests/test_agent_resource_api.py tests/test_agent_workbench.py && node --test tests/frontend/agent-workbench.test.cjs`
Expected: 全部通过。

- [x] **Step 2: 完整门禁**

Run: `.venv/bin/pytest -q && node --test tests/frontend/*.test.cjs && .venv/bin/ruff check app tests && .venv/bin/python -m compileall -q app tests && node --check app/static/app.js && git diff --check`
Expected: 所有命令退出 0。

- [x] **Step 3: 浏览器验收**

在隔离 Home 新建并编辑 MCP、Skill、Prompt、Profile，验证描述回读；拖动列表并刷新，顺序保持；验证 Provider 按客户端独立排序；在 1600px 和 390px 检查控制台、失败请求和横向溢出。

- [x] **Step 4: 文档与生产回灌**

更新 `docs/operations.md` 的新增/编辑/排序语义。在 Worktree 生成限定文件补丁，应用到原工作区，不触碰其他 1.7 万行既有修改；重启 8328/7888 并复验健康接口和生产页面。

- [x] **Step 5: 提交文档**

```bash
git add docs/operations.md docs/superpowers/plans/2026-07-29-agent-resource-create-description-order-plan.md
git commit -m "docs: document agent resource ordering"
```


### 执行记录

- [x] Task 1 提交：`b73e060`。
- [x] Task 2 提交：`adb65bd`。
- [x] Task 3 提交：`59f47c5`。
- [x] 定向门禁：Python `115 passed`，前端 `22 passed`。
- [x] 完整门禁：Python `462 passed`，前端 `30 passed`，其余静态检查退出 0。
- [x] 隔离 UI：五类新增、描述编辑、排序持久化、删除、Provider 客户端隔离、1600px/390px 均通过。
- [x] 生产回灌：14 个限定文件与 Worktree 字节一致；原工作区完整门禁 Python `462 passed`、前端 `30 passed`。
- [x] 生产运行态：8328/7888 HTTP 200；真实页面 5 个客户端、12 个 MCP、五类新增入口、1600px/390px 和浏览器错误采集通过。
