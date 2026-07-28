# Agent Database Resource Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 WSL Ops Panel 的 Agent 工作台改造成以 `data/state.db` 为主事实源的资源中心，并让 MCP、Skill、Prompt、Router、Profile 按客户端原生格式投影和回读。

**Architecture:** 在现有 `PanelStateStore` 上扩展幂等 Schema；MCP、Skill、Prompt 分别维护逻辑资源、客户端变体、目标分配和观察状态。Skill 内容进入 `data/agent/skills/<id>/` SSOT，Router 改为直接读取 SQLite，Profile 保存资源 ID 组合。客户端配置扫描只生成 discovery/observation，显式导入后才进入 managed library。

**Tech Stack:** Python 3.12/3.13、FastAPI、Pydantic、SQLite WAL、Jinja2、原生 JavaScript/CSS、pytest、Node test runner。

---

## 文件结构

**新建：**

- `app/services/agent_skill_store.py`：Skill 资源、SSOT、变体、分配与观察状态。
- `app/services/agent_profiles.py`：Profile CRUD、快照与最小差异计划。
- `app/services/agent_reconciler.py`：MCP/Skill/Prompt 的本地安装计划与状态归一化。
- `tests/test_agent_database_resources.py`：新 Schema、Skill、Prompt、MCP、Router、Profile 的数据库契约。
- `tests/test_agent_resource_api.py`：资源库与 reconcile API。

**修改：**

- `app/services/state_store.py`：新表、迁移标记及 CRUD。
- `app/services/agent_mcp.py`：显式导入保存变体与分配；库资源和观察项分离。
- `app/services/agent_prompts.py`：客户端变体、分配和观察。
- `app/services/agent_router_config.py`：`router.json` 一次性迁移，SQLite 主存储。
- `app/services/agent_workbench.py`：数据库主列表和 discovery 独立输出。
- `app/api/agent.py`：资源库、变体、导入、安装、卸载、同步、Profile API。
- `app/api/agent_router.py`：继续复用 Store API，响应来源改为 SQLite。
- `app/templates/agent_category.html`：资源库状态、发现区、安装/卸载/删库与 Profiles。
- `app/static/app.js`：资源操作、同步计划和状态刷新。
- `app/static/app.css`：紧凑资源卡、状态徽章和移动布局。
- `tests/test_state_store.py`、`tests/test_agent_workbench.py`、`tests/test_agent_skills_prompts.py`、`tests/test_agent_router.py`：回归契约。
- `tests/frontend/agent-workbench.test.cjs`：客户端筛选和资源动作。
- `docs/operations.md`：数据库、SSOT、迁移、备份和故障处理。

## Task 0: 隔离当前大规模未提交基线

**Files:**
- Create: global worktree `/home/div/.config/superpowers/worktrees/wsl-ops-panel/agent-database-center`
- Create: `/tmp/wsl-ops-panel-agent-db-baseline.patch`
- Create: `/tmp/wsl-ops-panel-agent-db-untracked.txt`

- [ ] **Step 1: 保存当前工作区 tracked 增量和相关 untracked 清单**

```bash
git diff --binary HEAD > /tmp/wsl-ops-panel-agent-db-baseline.patch
git ls-files --others --exclude-standard \
  | grep -E '^(app|tests|config|scripts|docs)/' \
  > /tmp/wsl-ops-panel-agent-db-untracked.txt
```

- [ ] **Step 2: 创建全局隔离 worktree**

```bash
git worktree add \
  /home/div/.config/superpowers/worktrees/wsl-ops-panel/agent-database-center \
  -b agent-database-center
```

- [ ] **Step 3: 在 worktree 恢复当前真实代码基线**

```bash
git apply /tmp/wsl-ops-panel-agent-db-baseline.patch
rsync -aR --files-from=/tmp/wsl-ops-panel-agent-db-untracked.txt ./ \
  /home/div/.config/superpowers/worktrees/wsl-ops-panel/agent-database-center/
```

- [ ] **Step 4: 提交隔离基线，后续只导出该提交之后的增量**

```bash
git add app tests config scripts docs pyproject.toml
git commit -m "chore: snapshot verified panel worktree baseline"
git rev-parse HEAD > /tmp/wsl-ops-panel-agent-db-baseline-commit
```

- [ ] **Step 5: 验证隔离基线**

```bash
.venv/bin/pytest -q
node --test tests/frontend/*.test.cjs
```

Expected: Python 与 Node 基线全部 PASS；若 worktree 未复制 `.venv`，使用原工作区的 `/home/div/1_Project_dir/AI/wsl-ops-panel/.venv/bin/pytest`。

## Task 1: 扩展 SQLite Schema 与 CRUD

**Files:**
- Modify: `app/services/state_store.py`
- Create: `tests/test_agent_database_resources.py`

- [ ] **Step 1: 写新表创建与幂等迁移的失败测试**

```python
def test_agent_resource_schema_is_created_idempotently(tmp_path: Path) -> None:
    first = PanelStateStore(tmp_path / "config")
    first.close()
    second = PanelStateStore(tmp_path / "config")
    names = second.list_table_names()
    assert {
        "agent_skills",
        "agent_skill_variants",
        "agent_skill_assignments",
        "agent_skill_observations",
        "agent_prompt_variants",
        "agent_prompt_assignments",
        "agent_prompt_observations",
        "agent_router_nodes",
        "agent_router_clients",
        "agent_route_failover_queue",
        "agent_profiles",
        "agent_profile_items",
        "agent_settings",
    } <= names
```

- [ ] **Step 2: 运行测试确认 RED**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py::test_agent_resource_schema_is_created_idempotently -q
```

Expected: FAIL，因为 `list_table_names` 或新表尚未存在。

- [ ] **Step 3: 在 `_ensure_schema()` 增加设计文档中的表、索引和设置表**

```python
CREATE TABLE IF NOT EXISTS agent_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    source TEXT,
    source_kind TEXT NOT NULL,
    ssot_path TEXT NOT NULL,
    version TEXT,
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

同一步加入 Skill 变体/分配/观察、Prompt 变体/分配/观察、Router、Profile 表及外键。

- [ ] **Step 4: 增加通用设置、表名和每类资源 CRUD**

```python
def get_agent_setting(self, key: str, default: Any = None) -> Any: ...
def set_agent_setting(self, key: str, value: Any) -> None: ...
def list_table_names(self) -> set[str]: ...

def upsert_skill(self, *, skill_id: str, name: str, ... ) -> dict[str, Any]: ...
def list_skills(self) -> dict[str, dict[str, Any]]: ...
def upsert_skill_variant(self, skill_id: str, client_id: str, platform: str, install: dict[str, Any]) -> dict[str, Any]: ...
def replace_skill_assignments(self, node_id: str, client_id: str, assignments: list[dict[str, Any]]) -> None: ...

def upsert_prompt_variant(self, prompt_id: str, client_id: str, platform: str, content: str, source: str) -> dict[str, Any]: ...
def set_prompt_assignment(self, node_id: str, client_id: str, prompt_id: str, variant_client_id: str, variant_platform: str) -> None: ...

def get_router_snapshot(self, node_id: str = "__local__") -> dict[str, Any]: ...
def replace_router_snapshot(self, node_id: str, payload: dict[str, Any]) -> None: ...

def upsert_profile(self, profile_id: str, name: str, description: str | None, items: list[dict[str, Any]]) -> dict[str, Any]: ...
def list_profiles(self) -> list[dict[str, Any]]: ...
```

- [ ] **Step 5: 写并运行 CRUD、外键、级联删除和事务测试**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py tests/test_state_store.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/services/state_store.py tests/test_agent_database_resources.py tests/test_state_store.py
git commit -m "feat: add agent resource database schema"
```

## Task 2: 建立 Skill 资源库和 SSOT

**Files:**
- Create: `app/services/agent_skill_store.py`
- Modify: `app/services/agent_skills.py`
- Modify: `tests/test_agent_database_resources.py`
- Modify: `tests/test_agent_skills_prompts.py`

- [ ] **Step 1: 写 Skill 导入数据库而不直接安装客户端的失败测试**

```python
def test_skill_import_creates_ssot_without_touching_client(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    store = AgentSkillStore(tmp_path / "data/agent")

    saved = store.import_source("demo", "Demo", str(source))

    assert Path(saved["ssot_path"], "SKILL.md").read_text() == "# Demo\n"
    assert not (tmp_path / "home/.codex/skills/demo").exists()
    assert AgentSkillStore(tmp_path / "data/agent").get("demo")["content_hash"]
```

- [ ] **Step 2: 运行确认 RED**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py::test_skill_import_creates_ssot_without_touching_client -q
```

Expected: FAIL，因为 `AgentSkillStore` 尚未存在。

- [ ] **Step 3: 实现 `AgentSkillStore`**

```python
class AgentSkillStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.ssot_root = self.data_root / "skills"
        self.state = PanelStateStore(self.data_root)

    def import_source(self, skill_id: str, name: str, source: str, *, description: str | None = None) -> dict[str, Any]:
        # 复用安全物化、SKILL.md 校验、稳定目录 Hash、同目录临时 staging 和原子替换。
        ...

    def install_to_client(self, home: Path, skill_id: str, client_id: str, *, mode: str = "copy") -> dict[str, Any]: ...
    def uninstall_from_client(self, home: Path, skill_id: str, client_id: str) -> dict[str, Any]: ...
    def import_from_client(self, home: Path, client_id: str, skill_name: str) -> dict[str, Any]: ...
    def sync_client(self, home: Path, client_id: str) -> dict[str, list[str]]: ...
```

- [ ] **Step 4: 让客户端安装只从 SSOT 读取并更新 assignment/observation**

安装成功后写入：

```python
state.replace_skill_assignments("__local__", client_id, [...])
state.replace_skill_observations("__local__", client_id, [...])
```

卸载只移除目标 assignment/observation，保留 `agent_skills` 与 SSOT。

- [ ] **Step 5: 覆盖 copy、symlink、更新、卸载、Hash 漂移、删库级联测试**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py tests/test_agent_skills_prompts.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/services/agent_skill_store.py app/services/agent_skills.py tests/test_agent_database_resources.py tests/test_agent_skills_prompts.py
git commit -m "feat: add database-backed skill library"
```

## Task 3: Prompt 客户端变体、分配和观察

**Files:**
- Modify: `app/services/agent_prompts.py`
- Modify: `tests/test_agent_database_resources.py`
- Modify: `tests/test_agent_skills_prompts.py`

- [ ] **Step 1: 写客户端变体优先、基础内容回退的失败测试**

```python
def test_prompt_resolves_client_variant_then_base_content(tmp_path: Path) -> None:
    store = AgentPromptStore(tmp_path / "data/agent")
    store.upsert_prompt("rules", "Rules", "base\n")
    store.upsert_variant("rules", "claude", "linux", "claude\n")
    assert store.resolve_content("rules", "claude", "linux") == "claude\n"
    assert store.resolve_content("rules", "codex", "linux") == "base\n"
```

- [ ] **Step 2: 运行确认 RED**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py::test_prompt_resolves_client_variant_then_base_content -q
```

Expected: FAIL，因为变体 API 尚未存在。

- [ ] **Step 3: 扩展 `AgentPromptStore`**

```python
def upsert_variant(self, prompt_id: str, client_id: str, platform: str, content: str, *, source: str = "manual") -> dict[str, Any]: ...
def resolve_content(self, prompt_id: str, client_id: str, platform: str = "linux") -> str: ...
def assign(self, node_id: str, client_id: str, prompt_id: str, *, platform: str = "linux") -> None: ...
def install_local(self, home: Path, client_id: str, prompt_id: str) -> dict[str, Any]: ...
def uninstall_local(self, home: Path, client_id: str) -> dict[str, Any]: ...
```

`install_local` 复用 `AgentPromptFileManager` 的备份、原子写和冲突检测，并在回读后写 observation。

- [ ] **Step 4: 让“导入当前 Prompt”创建数据库资源和来源客户端变体**

导入 ID 采用用户提交 ID；缺省 ID 使用 `<client_id>-current`，并保存 assignment 与 installed observation，不回写文件。

- [ ] **Step 5: 运行 Prompt 全部测试**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py tests/test_agent_skills_prompts.py tests/test_agent_workbench.py -k 'prompt' -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/services/agent_prompts.py tests/test_agent_database_resources.py tests/test_agent_skills_prompts.py tests/test_agent_workbench.py
git commit -m "feat: add prompt variants and assignments"
```

## Task 4: MCP 改为数据库主列表和显式导入

**Files:**
- Modify: `app/services/agent_mcp.py`
- Modify: `app/services/agent_workbench.py`
- Modify: `tests/test_agent_database_resources.py`
- Modify: `tests/test_agent_workbench.py`

- [ ] **Step 1: 写 observed 项不进入 managed library 的失败测试**

```python
def test_workbench_keeps_observed_mcp_outside_library(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_codex_mcp(home, "native-only", {"command": "node"})
    context = build_agent_workbench_context(tmp_path / "config", home=home, which=lambda _: None)
    assert context["agent_mcp_servers"] == []
    assert context["agent_mcp_discovery"][0]["id"] == "native-only"
```

- [ ] **Step 2: 运行确认 RED**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py::test_workbench_keeps_observed_mcp_outside_library -q
```

Expected: FAIL，当前工作台会把 observed MCP 拼进主列表。

- [ ] **Step 3: 修改 MCP 导入保存逻辑资源、客户端变体、assignment 和 observation**

```python
def import_client(self, home: Path, client_id: str, *, node_id: str = "__local__", platform: str = "linux") -> int:
    scanned = scan_mcp_home(home, client_id)
    for mcp_id, spec in scanned.items():
        self._state.upsert_mcp_server(...)
        self._state.upsert_mcp_variant(mcp_id, client_id, platform, spec, source="import")
    self._state.replace_mcp_assignments(...)
    self._state.replace_mcp_observations(...)
    return len(scanned)
```

同名跨客户端配置保留一个逻辑 ID和多个 variant，不再创建 `id--client` 伪资源。

- [ ] **Step 4: 安装时优先解析客户端 variant，缺省回退 base spec**

```python
def resolve_server_for_client(self, mcp_id: str, client_id: str, platform: str = "linux") -> dict[str, Any]: ...
```

安装成功更新 assignment；卸载移除当前目标 assignment，保留资源库记录。

- [ ] **Step 5: 工作台输出 managed 与 discovery 两个集合**

```python
return {
    "agent_mcp_servers": managed_rows,
    "agent_mcp_discovery": discovered_rows,
    ...
}
```

- [ ] **Step 6: 运行 MCP 回归**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py tests/test_state_store.py tests/test_agent_workbench.py -k 'mcp or workbench' -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add app/services/agent_mcp.py app/services/agent_workbench.py tests/test_agent_database_resources.py tests/test_agent_workbench.py
git commit -m "feat: make MCP library database authoritative"
```

## Task 5: Router 从 JSON 迁移到 SQLite

**Files:**
- Modify: `app/services/agent_router_config.py`
- Modify: `tests/test_agent_router.py`
- Modify: `tests/test_agent_database_resources.py`

- [ ] **Step 1: 写一次性迁移和重启持久化失败测试**

```python
def test_router_json_migrates_once_then_sqlite_wins(tmp_path: Path) -> None:
    data_root = tmp_path / "data/agent"
    data_root.mkdir(parents=True)
    legacy = data_root / "router.json"
    legacy.write_text(json.dumps({"listen_port": 7999, "takeover": {"codex": True}}))
    first = AgentRouterConfigStore(data_root)
    assert first.snapshot()["listen_port"] == 7999
    first.update_global(listen_port=7888)
    legacy.write_text(json.dumps({"listen_port": 7000}))
    assert AgentRouterConfigStore(data_root).snapshot()["listen_port"] == 7888
```

- [ ] **Step 2: 运行确认 RED**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py::test_router_json_migrates_once_then_sqlite_wins -q
```

Expected: FAIL，当前每次都读取 JSON。

- [ ] **Step 3: 保留 Store 公共 API，底层改用 `PanelStateStore`**

```python
class AgentRouterConfigStore:
    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "router.json"  # 仅兼容迁移来源
        self._state = PanelStateStore(self.data_root)
        self._migrate_legacy_json_once()

    def _read(self) -> dict[str, Any]:
        return _with_defaults(self._state.get_router_snapshot("__local__"))

    def _write(self, payload: dict[str, Any]) -> None:
        self._state.replace_router_snapshot("__local__", _with_defaults(payload))
```

- [ ] **Step 4: 将 Provider、当前 ID、takeover、策略和 failover queue 映射到 Router 表**

`agent_router_clients` 保存 per-client 状态，`agent_route_failover_queue` 保存顺序；运行时 snapshot 继续保持现有字典形状，避免 Router 和 API 上层重写。

- [ ] **Step 5: 更新旧文件权限测试为数据库与迁移文件权限测试**

数据库必须 `0600`；旧 `router.json` 保留不再更新，迁移标记为 `router_json_migrated_v1`。

- [ ] **Step 6: 运行 Router 全部回归**

```bash
.venv/bin/pytest tests/test_agent_router.py tests/test_agent_route_takeover.py tests/test_agent_database_resources.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add app/services/agent_router_config.py tests/test_agent_router.py tests/test_agent_database_resources.py
git commit -m "feat: persist agent router configuration in sqlite"
```

## Task 6: Profile 与统一 reconcile 计划

**Files:**
- Create: `app/services/agent_profiles.py`
- Create: `app/services/agent_reconciler.py`
- Modify: `tests/test_agent_database_resources.py`

- [ ] **Step 1: 写 Profile CRUD 和最小差异计划失败测试**

```python
def test_profile_plan_only_changes_different_resources(tmp_path: Path) -> None:
    profile_store = AgentProfileStore(tmp_path / "data/agent")
    profile_store.upsert("work", "Work", items=[
        {"client_id": "codex", "resource_type": "mcp", "resource_id": "context7"},
        {"client_id": "codex", "resource_type": "skill", "resource_id": "review"},
    ])
    plan = AgentReconciler(tmp_path / "data/agent").plan_profile(
        "work", node_id="__local__", observations={"mcp": {"context7": "installed"}}
    )
    assert [item.resource_id for item in plan.operations] == ["review"]
```

- [ ] **Step 2: 运行确认 RED**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py::test_profile_plan_only_changes_different_resources -q
```

Expected: FAIL，因为 Profile/Reconciler 尚未存在。

- [ ] **Step 3: 实现 Profile Store**

```python
class AgentProfileStore:
    def list(self) -> list[dict[str, Any]]: ...
    def get(self, profile_id: str) -> dict[str, Any] | None: ...
    def upsert(self, profile_id: str, name: str, *, description: str | None = None, items: list[dict[str, Any]]) -> dict[str, Any]: ...
    def delete(self, profile_id: str) -> bool: ...
    def snapshot_client(self, profile_id: str, client_id: str) -> dict[str, Any]: ...
```

- [ ] **Step 4: 实现只生成明确 operation 的 Reconciler**

```python
@dataclass(frozen=True)
class AgentResourceOperation:
    action: Literal["install", "update", "uninstall"]
    resource_type: Literal["mcp", "skill", "prompt"]
    resource_id: str
    client_id: str
    node_id: str

@dataclass(frozen=True)
class AgentResourcePlan:
    operations: list[AgentResourceOperation]
    already_consistent: list[str]
    warnings: list[str]
```

- [ ] **Step 5: 覆盖 dangling ID、无差异、漂移、每客户端隔离测试**

```bash
.venv/bin/pytest tests/test_agent_database_resources.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add app/services/agent_profiles.py app/services/agent_reconciler.py tests/test_agent_database_resources.py
git commit -m "feat: add agent profiles and reconciliation plans"
```

## Task 7: 资源库 API 和本地执行链

**Files:**
- Modify: `app/api/agent.py`
- Create: `tests/test_agent_resource_api.py`
- Modify: `tests/test_agent_workbench.py`

- [ ] **Step 1: 写资源 API 失败测试**

覆盖：

```text
GET    /api/agent/library
POST   /api/agent/mcp/import-current
POST   /api/agent/skills/import-current
POST   /api/agent/prompts/import-current
POST   /api/agent/resources/plan
POST   /api/agent/resources/reconcile
DELETE /api/agent/resources/{type}/{id}
GET    /api/agent/profiles
PUT    /api/agent/profiles/{id}
POST   /api/agent/profiles/{id}/apply
DELETE /api/agent/profiles/{id}
```

示例：

```python
def test_skill_library_import_then_install_and_uninstall(tmp_path: Path, monkeypatch) -> None:
    response = client.post("/api/agent/skills/import-source", json={...})
    assert response.status_code == 200
    install = client.post("/api/agent/resources/reconcile", json={...})
    assert install.json()["verified"] is True
    uninstall = client.post("/api/agent/resources/reconcile", json={...})
    assert uninstall.json()["removed"] == ["demo"]
    assert client.get("/api/agent/library").json()["skills"][0]["id"] == "demo"
```

- [ ] **Step 2: 运行确认 RED**

```bash
.venv/bin/pytest tests/test_agent_resource_api.py -q
```

Expected: FAIL，路由尚未存在。

- [ ] **Step 3: 实现 Pydantic 请求模型和认证 API**

请求只接受受支持的 `resource_type`、`client_id`、`node_id=__local__` 和安全 ID。响应返回脱敏资源、计划、回读状态和零操作说明。

- [ ] **Step 4: 保留旧 API 兼容，内部改调新 Store**

旧 `/mcp/local/install`、`/skills/local/install`、`/prompts/local/apply` 继续工作，但都更新数据库 assignment/observation。旧远程队列 API保持原语义。

- [ ] **Step 5: 从库删除执行“先卸载全部本地分配，再删资源”**

任一卸载失败返回 `409` 与失败目标，数据库资源保持。无分配时直接删除资源和 Skill SSOT。

- [ ] **Step 6: 运行 API 回归**

```bash
.venv/bin/pytest tests/test_agent_resource_api.py tests/test_agent_workbench.py tests/test_agent_skills_prompts.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交**

```bash
git add app/api/agent.py tests/test_agent_resource_api.py tests/test_agent_workbench.py tests/test_agent_skills_prompts.py
git commit -m "feat: expose agent resource library APIs"
```

## Task 8: 数据库主导的 UI

**Files:**
- Modify: `app/services/agent_workbench.py`
- Modify: `app/templates/agent_category.html`
- Modify: `app/static/app.js`
- Modify: `app/static/app.css`
- Modify: `tests/frontend/agent-workbench.test.cjs`
- Modify: `tests/test_agent_workbench.py`

- [ ] **Step 1: 写前端结构和状态失败测试**

```javascript
test('resource library separates managed and discovered items', () => {
  assert.match(template, /data-agent-library/)
  assert.match(template, /data-agent-discovery/)
  assert.match(template, /data-agent-resource-install/)
  assert.match(template, /data-agent-resource-uninstall/)
  assert.match(template, /data-agent-resource-delete/)
  assert.match(template, /data-agent-resource-sync/)
})
```

- [ ] **Step 2: 运行确认 RED**

```bash
node --test tests/frontend/agent-workbench.test.cjs
```

Expected: FAIL，因为新 data attributes 尚未存在。

- [ ] **Step 3: 调整模板为资源库主视图**

每个 MCP/Skill/Prompt 卡片展示：

```text
名称 | 来源 | 库中
当前客户端：未分配 / 已分配
回读：已安装 / 漂移 / 缺失 / 错误
编辑 | 安装/更新 | 卸载 | 按库同步 | 从库删除
```

发现区只展示 observed 项和“导入到库”按钮。

- [ ] **Step 4: 新增 Profiles Tab**

支持新建、编辑资源组合、应用与删除；目标客户端继续使用页面顶部唯一选择器。

- [ ] **Step 5: 更新 JavaScript**

所有动作调用新 API；操作完成后刷新 library inventory。无差异时显示“已与数据库一致”，不展示虚假 success 数量。

- [ ] **Step 6: 更新 CSS**

按钮等高、紧凑、亮暗态明确；卡片使用稳定网格；1600px 与 390px 不横向溢出。

- [ ] **Step 7: 运行前端和模板测试**

```bash
node --test tests/frontend/*.test.cjs
.venv/bin/pytest tests/test_agent_workbench.py -q
node --check app/static/app.js
```

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add app/services/agent_workbench.py app/templates/agent_category.html app/static/app.js app/static/app.css tests/frontend/agent-workbench.test.cjs tests/test_agent_workbench.py
git commit -m "feat: render database-backed agent resource center"
```

## Task 9: 迁移、文档和端到端验收

**Files:**
- Modify: `docs/operations.md`
- Modify: `agent-audit-20260713-130739.md`
- Create: `artifacts/agent-database-center-desktop.png`
- Create: `artifacts/agent-database-center-mobile.png`

- [ ] **Step 1: 在隔离数据库复制上验证真实迁移**

```bash
cp data/state.db /tmp/wsl-ops-state-before-agent-db.sqlite3
cp data/state.db /tmp/wsl-ops-state-migration-test.sqlite3
python - <<'PY'
from app.services.state_store import PanelStateStore
store = PanelStateStore('/tmp/wsl-ops-state-migration-test.sqlite3')
print(sorted(store.list_table_names()))
PY
```

Expected: 新表齐全，原有 MCP/Provider/Prompt 数据行数保持。

- [ ] **Step 2: 验证隔离 HOME 六客户端完整链路**

运行 API/服务测试覆盖 Claude、Codex、Gemini、Grok Build、OpenCode、Hermes 的 MCP、Skill、Prompt 导入、安装、更新、卸载和回读；OpenClaw 只显示真实能力。

- [ ] **Step 3: 完整代码门禁**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
.venv/bin/python -m compileall -q app tests
node --test tests/frontend/*.test.cjs
node --check app/static/app.js
git diff --check
```

Expected: 全部退出码 0。

- [ ] **Step 4: 更新运维文档和审计交接**

写明：

- `state.db` 是 Agent 业务事实源。
- Skill SSOT 路径和备份恢复。
- `router.json` 一次性迁移。
- 导入、安装、卸载、同步、从库删除的差异。
- 数据库备份和回滚步骤。

- [ ] **Step 5: 将 worktree 增量应用回原工作区**

```bash
baseline=$(cat /tmp/wsl-ops-panel-agent-db-baseline-commit)
git diff --binary "$baseline"..HEAD > /tmp/wsl-ops-panel-agent-db-implementation.patch
cd /home/div/1_Project_dir/AI/wsl-ops-panel
git apply --check /tmp/wsl-ops-panel-agent-db-implementation.patch
git apply /tmp/wsl-ops-panel-agent-db-implementation.patch
```

- [ ] **Step 6: 在原工作区重新运行完整门禁**

```bash
.venv/bin/pytest -q
.venv/bin/ruff check app tests
.venv/bin/python -m compileall -q app tests
node --test tests/frontend/*.test.cjs
node --check app/static/app.js
git diff --check
```

Expected: 全部退出码 0。

- [ ] **Step 7: 重启服务并现场验收**

```bash
systemctl --user restart wsl-ops-panel.service wsl-agent-router.service
systemctl --user is-active wsl-ops-panel.service wsl-agent-router.service
curl -fsS http://127.0.0.1:8328/healthz
curl -fsS http://127.0.0.1:7888/health
```

用浏览器验证 `/categories/agent` 桌面与移动布局、资源库/发现区、安装/卸载/同步/删库、Profiles，并保存截图。

- [ ] **Step 8: 最终差异与需求逐项核对**

```bash
git status --short
git diff --stat
git diff --check
```

确认原有大规模未提交改动仍在，未执行整体回滚或批量覆盖。
