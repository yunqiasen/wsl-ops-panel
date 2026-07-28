# Agent 数据库资源中心设计

**日期：** 2026-07-29  
**状态：** 已确认  
**范围：** 当前本地 WSL（`node_id=__local__`）  
**参考：** CC Switch 当前主分支的 SQLite、MCP、Skill、Prompt、Profile 与 Proxy 实现

## 1. 背景与结论

当前 Agent 工作台已有 SQLite 基础，但还处于“数据库记录 + 客户端现场扫描 + JSON 运行配置”混合状态。页面会把客户端原生文件中的观察结果与数据库资源混在一起，Skill 仍直接扫描和修改客户端目录，Router 仍以 `data/agent/router.json` 为主存储，因此整体行为更像客户端操作台，而不是 CC Switch 的资源中心。

目标是将 `data/state.db` 设为 Agent 配置的唯一业务事实源：Provider、MCP、Skill 元数据、Prompt、Profile、路由策略和客户端分配均由数据库管理；Skill 内容使用统一 SSOT 目录；各客户端原生配置文件只作为安装投影与回读对象。

CC Switch 对 Skill 采用的也是“数据库元数据 + 统一内容目录 + 客户端投影”，并非把整个 Skill 目录作为 SQLite BLOB 保存。本项目沿用这一结构，同时保留未来多设备所需的 `node_id` 维度。

## 2. 目标

1. 数据库资源与客户端现场状态严格分离。
2. MCP、Skill、Prompt 均支持“资源库 → 客户端变体 → 目标分配 → 安装投影 → 回读状态”。
3. Provider、路由、接管和故障转移配置在数据库中持久化。
4. 不同客户端继续使用独立原生适配器，保留未知字段并执行备份、原子写入和回读。
5. UI 明确区分导入、安装、卸载、按库同步和从库删除。
6. 现有 `state.db`、MCP 数据、客户端原生文件及 Router 配置平滑迁移。
7. 本阶段只处理本地 WSL；数据库模型保留 `node_id`，后续 SSH 直接复用。

## 3. 本阶段范围外

- SSH 节点执行与远程文件传输。
- CC Switch 的 WebDAV、S3、云同步、托盘菜单和自动更新。
- Skill 商店、在线排行与仓库搜索页面。
- 代理请求计费、统计图表和完整熔断观测平台。
- OpenClaw 尚未具备原生能力的 MCP/Skill 投影。

这些能力不会进入本轮数据库化主链路。

## 4. 事实源与状态模型

### 4.1 四层状态

```text
数据库资源库
  ├── Provider
  ├── MCP
  ├── Skill
  ├── Prompt
  ├── Router
  └── Profile
        ↓
客户端专属变体
        ↓
目标分配（__local__ + client_id）
        ↓
客户端原生配置 / Skill 目录
        ↓
回读观察与操作记录
```

各层职责如下：

- **资源库：** 用户管理的逻辑资源，页面列表默认只从这里读取。
- **客户端变体：** 同一逻辑资源针对 Claude、Codex、Gemini、Grok Build、OpenCode、Hermes 的差异配置。
- **目标分配：** 记录某个节点和客户端期望启用哪些资源。
- **原生投影：** 适配器生成并写入客户端真实格式。
- **观察状态：** 扫描客户端文件得到的只读事实，不自动伪装成数据库资源。

### 4.2 状态词义

- `library`：资源已保存到数据库。
- `assigned`：资源已分配给目标客户端。
- `installed`：回读结果与分配的期望内容一致。
- `drifted`：客户端存在对应项，但内容 Hash 与期望不同。
- `missing`：数据库期望启用，客户端回读未发现。
- `observed`：客户端现场存在，尚未显式导入资源库。
- `error`：解析、写入或回读失败，并保存错误摘要。

## 5. 数据库设计

继续使用：

```text
/home/div/1_Project_dir/AI/wsl-ops-panel/data/state.db
```

数据库连接继续启用 WAL、外键和 busy timeout。Schema 初始化与迁移保持幂等。

### 5.1 保留表

- `agent_providers`
- `agent_mcp_servers`
- `agent_mcp_variants`
- `agent_mcp_assignments`
- `agent_mcp_observations`
- `agent_mcp_operations`
- `agent_prompts`

Provider 密钥继续保存在受限 Secret Store，数据库只保存 `secret_ref`。页面与普通 API 只返回脱敏值。

### 5.2 Skill 资源表

新增 `agent_skills`：

```text
id                TEXT PRIMARY KEY
name              TEXT NOT NULL
description       TEXT
source            TEXT
source_kind       TEXT NOT NULL
ssot_path         TEXT NOT NULL
version           TEXT
content_hash      TEXT NOT NULL
metadata_json     TEXT NOT NULL DEFAULT '{}'
created_at        TEXT NOT NULL
updated_at        TEXT NOT NULL
```

新增 `agent_skill_variants`：

```text
skill_id          TEXT NOT NULL
client_id         TEXT NOT NULL
platform          TEXT NOT NULL
install_json      TEXT NOT NULL DEFAULT '{}'
created_at        TEXT NOT NULL
updated_at        TEXT NOT NULL
PRIMARY KEY (skill_id, client_id, platform)
FOREIGN KEY (skill_id) REFERENCES agent_skills(id) ON DELETE CASCADE
```

`install_json` 只保存客户端差异，例如安装目录名、同步方式和客户端专属文件选择，不重复保存 Skill 内容。

新增 `agent_skill_assignments`：

```text
node_id           TEXT NOT NULL
client_id         TEXT NOT NULL
skill_id          TEXT NOT NULL
variant_client_id TEXT NOT NULL
variant_platform  TEXT NOT NULL
desired_enabled   INTEGER NOT NULL DEFAULT 1
updated_at        TEXT NOT NULL
PRIMARY KEY (node_id, client_id, skill_id)
FOREIGN KEY (skill_id) REFERENCES agent_skills(id) ON DELETE CASCADE
```

新增 `agent_skill_observations`，字段与 MCP 观察表保持相同语义：目标、资源 ID、是否存在、内容 Hash、状态、扫描时间和错误。

### 5.3 Skill SSOT 目录

统一内容目录：

```text
/home/div/1_Project_dir/AI/wsl-ops-panel/data/agent/skills/<skill_id>/
```

导入或安装 Skill 到资源库时：

1. 将来源物化到临时目录。
2. 校验安全路径和 `SKILL.md`。
3. 计算稳定目录 Hash。
4. 原子替换 SSOT 目录。
5. 在数据库事务中写入元数据。
6. 根据目标分配投影到客户端目录。

客户端目录不再作为 Skill 内容主来源。

### 5.4 Prompt 客户端变体

保留 `agent_prompts` 作为逻辑资源，新增：

`agent_prompt_variants`：

```text
prompt_id         TEXT NOT NULL
client_id         TEXT NOT NULL
platform          TEXT NOT NULL
content           TEXT NOT NULL
source            TEXT NOT NULL
created_at        TEXT NOT NULL
updated_at        TEXT NOT NULL
PRIMARY KEY (prompt_id, client_id, platform)
FOREIGN KEY (prompt_id) REFERENCES agent_prompts(id) ON DELETE CASCADE
```

`agent_prompt_assignments`：

```text
node_id           TEXT NOT NULL
client_id         TEXT NOT NULL
prompt_id         TEXT NOT NULL
variant_client_id TEXT NOT NULL
variant_platform  TEXT NOT NULL
desired_enabled   INTEGER NOT NULL DEFAULT 1
updated_at        TEXT NOT NULL
PRIMARY KEY (node_id, client_id)
FOREIGN KEY (prompt_id) REFERENCES agent_prompts(id) ON DELETE CASCADE
```

每个目标客户端同一时间只有一个期望启用的 Prompt。未提供客户端变体时回退到逻辑资源的基础内容。

新增 `agent_prompt_observations`，记录目标文件 Hash、当前状态及外部修改冲突。

### 5.5 Router 数据库表

新增 `agent_router_nodes`：

```text
node_id            TEXT PRIMARY KEY
listen_address     TEXT NOT NULL
listen_port        INTEGER NOT NULL
show_home_switch   INTEGER NOT NULL DEFAULT 1
outbound_proxy_ref TEXT
updated_at         TEXT NOT NULL
```

新增 `agent_router_clients`：

```text
node_id            TEXT NOT NULL
client_id          TEXT NOT NULL
provider_id        TEXT
enabled             INTEGER NOT NULL DEFAULT 0
takeover_enabled    INTEGER NOT NULL DEFAULT 0
auto_failover       INTEGER NOT NULL DEFAULT 0
max_retries         INTEGER NOT NULL DEFAULT 0
failure_threshold   INTEGER NOT NULL DEFAULT 3
cooldown_seconds    INTEGER NOT NULL DEFAULT 60
updated_at          TEXT NOT NULL
PRIMARY KEY (node_id, client_id)
```

新增 `agent_route_failover_queue`：

```text
node_id           TEXT NOT NULL
client_id         TEXT NOT NULL
provider_id       TEXT NOT NULL
sort_index        INTEGER NOT NULL
PRIMARY KEY (node_id, client_id, provider_id)
```

Router 进程直接读取数据库快照。`router.json` 仅作为一次性迁移来源，不再承担主存储职责。路由密钥和带认证信息的出站代理继续使用 Secret Store 引用。

### 5.6 Profile

新增 `agent_profiles`：

```text
id                TEXT PRIMARY KEY
name              TEXT NOT NULL
description       TEXT
created_at        TEXT NOT NULL
updated_at        TEXT NOT NULL
```

新增 `agent_profile_items`：

```text
profile_id        TEXT NOT NULL
client_id         TEXT NOT NULL
resource_type     TEXT NOT NULL
resource_id       TEXT NOT NULL
config_json       TEXT NOT NULL DEFAULT '{}'
sort_index        INTEGER NOT NULL DEFAULT 0
PRIMARY KEY (profile_id, client_id, resource_type, resource_id)
FOREIGN KEY (profile_id) REFERENCES agent_profiles(id) ON DELETE CASCADE
```

`resource_type` 只接受 `provider`、`mcp`、`skill`、`prompt`、`router`。Profile 保存资源 ID 和少量组合覆盖，不复制密钥或整个 Skill 内容。

### 5.7 旧表迁移

- `agent_mcp_targets`：迁移为 `agent_mcp_assignments`，之后只保留兼容读取。
- `agent_mcp_variants`：继续使用并补齐导入链路，成为客户端差异主来源。
- `agent_mcp_observations`：只表示回读结果，不再直接进入资源库主列表。
- `agent_prompts`：现有记录作为基础 Prompt，客户端变体按需补充。
- `data/agent/router.json`：启动迁移时一次性导入 Router 表，并记录迁移标记。
- 客户端 Skill 目录：只在用户执行“从当前客户端导入”时进入 SSOT 和数据库。

本轮迁移不删除旧表、原生配置或历史备份。完成一个发布周期后再单独清理兼容字段。

## 6. 服务边界

### 6.1 资源 Store

每类资源提供统一语义：

```text
list / get / upsert / delete
list_variants / upsert_variant
list_assignments / replace_assignments
list_observations / replace_observations
```

`PanelStateStore` 负责 SQL 和事务；资源 Store 负责验证、迁移和领域对象组装。

### 6.2 客户端适配器

每个客户端继续实现独立适配器：

```text
scan(home) -> observations
import_native(home) -> canonical resource + client variant
plan(desired, observed) -> operations
apply(operation) -> write result
verify(expected) -> observation
```

适配器必须：

- 使用客户端真实路径和格式。
- 保留未受管字段。
- 写入前备份。
- 使用原子替换。
- 回读并比较完整投影。
- 日志只记录资源 ID、目标和结果，不记录密钥与完整配置载荷。

### 6.3 安装协调器

新增统一协调器负责：

1. 读取数据库资源、变体和目标分配。
2. 生成可预览的安装计划。
3. 串行提交到现有全局任务队列。
4. 调用对应客户端适配器。
5. 回读并更新 observation、operation。
6. 失败时保留数据库资源和期望状态，页面显示错误并允许重试。

## 7. 业务流程

### 7.1 从当前客户端导入

```text
读取原生配置
→ 转换为标准资源
→ 保存逻辑资源
→ 保存来源客户端变体
→ 保存来源客户端分配
→ 写入已安装 observation
```

导入只读取现场并写数据库，不反向重写客户端文件。

### 7.2 安装或更新

```text
选择数据库资源和目标客户端
→ 解析最具体变体
→ 生成变更计划
→ 备份原生文件
→ 原子写入
→ 回读完整投影
→ 更新 assignment / observation / operation
```

同一资源只写入用户选中的客户端。客户端选择器用于过滤与目标选择，不改变其他客户端状态。

### 7.3 卸载

卸载只删除目标客户端中的受管投影，并将对应 assignment 设为禁用或移除。数据库资源与 SSOT 内容继续保留。

### 7.4 从库删除

从库删除与卸载分开：

1. 查询全部启用分配。
2. 为这些目标生成卸载计划。
3. 全部回读成功后删除数据库资源与 SSOT 内容。
4. 任一目标失败时保留资源记录，展示失败目标，避免出现数据库已删而客户端仍残留的状态。

### 7.5 按库同步

“按库同步”比较 assignments 与 observations：

- 期望启用且缺失：安装。
- 期望启用且漂移：更新。
- 期望禁用但现场存在：卸载。
- 无 assignment 的 observed 项：仅显示为“未导入”，不自动删除。

没有分配或差异时返回明确的“已一致”，同时记录零操作结果。

### 7.6 应用 Profile

应用 Profile 时按客户端计算 Provider、MCP、Skill、Prompt 和 Router 的最小差异，形成单个可审计计划。单项失败进入 warnings，未执行项保持原状；数据库 Profile 本身不受影响。

## 8. UI 设计

顶部继续保留唯一客户端选择器。主要标签为：

```text
Providers | Route | MCP | Skills | Prompts | Profiles
```

MCP、Skills、Prompts 页面默认展示数据库资源库。客户端现场扫描结果放入独立的“发现”区域或导入弹层。

资源卡片统一展示：

- 资源名和来源。
- 库中状态。
- 当前客户端的分配状态。
- 回读状态：已安装、漂移、缺失、错误。
- 客户端变体标识。

统一动作：

- 编辑资源。
- 从当前客户端导入。
- 安装 / 更新。
- 卸载。
- 按库同步。
- 从库删除。

按钮语义：

- **从当前客户端导入：** 原生配置进入数据库。
- **安装 / 更新：** 数据库资源投影到当前客户端。
- **卸载：** 仅移除当前客户端投影。
- **按库同步：** 根据分配与回读差异执行操作。
- **从库删除：** 完成相关目标卸载后删除数据库资源。

客户端按钮保持等宽、单选、亮暗态。资源选择使用普通按钮或卡片高亮，不使用原生复选框勾选作为主交互。

## 9. 错误处理与一致性

- 数据库变更使用事务。
- 文件写入使用“备份 → 临时文件 → fsync → 原子替换 → 回读”。
- 数据库 assignment 在执行前保存期望状态；observation 只在回读后更新。
- 文件写入失败时恢复原文件，并记录 operation error。
- 外部修改导致 Hash 冲突时标记 `drifted`，保留现场内容。
- Skill SSOT 更新失败时保留旧目录和旧 Hash。
- Router 数据迁移成功后写入迁移标记；重复启动不重复覆盖数据库。
- 列表 API 默认不返回密钥、完整 Secret Store 内容或未脱敏环境变量。

## 10. 迁移顺序

1. 备份 `state.db` 与 `router.json`。
2. 创建新表和索引。
3. 迁移 MCP targets、variants 与 assignments。
4. 迁移基础 Prompt。
5. 导入 Router JSON 到数据库。
6. 提供显式 Skill 导入入口，将选中的客户端 Skill 复制到 SSOT。
7. 切换工作台列表为数据库资源主列表。
8. 将扫描结果改为 observations / discovery。
9. 启用安装协调器和按库同步。
10. 完成 Profile 与 UI。

迁移过程只增加或复制数据，不批量改写当前客户端原生文件。原生写入只由用户执行安装、同步、卸载或应用 Profile 触发。

## 11. 测试与验收

### 11.1 数据库

- 新数据库创建全部表。
- 旧数据库幂等迁移。
- `router.json` 只导入一次。
- MCP 旧 targets 正确进入 assignments。
- 级联删除和事务回滚符合设计。
- 重启后资源、分配、Profile 和 Router 状态保持一致。

### 11.2 资源服务

- Skill 导入后 SSOT、Hash 和数据库记录一致。
- Skill 客户端目录删除后，资源库记录仍存在。
- Prompt 基础内容与客户端变体正确回退。
- MCP 同名不同客户端配置保存为变体，不相互覆盖。
- observed 项在显式导入前不会成为 managed 资源。

### 11.3 原生适配器

在隔离 HOME 中覆盖：

- Claude Code
- Codex
- Gemini CLI
- Grok Build
- OpenCode
- Hermes

每个支持项执行导入、安装、更新、卸载、同步、回读与未知字段保留测试。OpenClaw 只验证其真实能力矩阵。

### 11.4 API 与 UI

- 导入、资源 CRUD、分配、计划、安装、卸载、同步、Profile API。
- 未检测客户端、无变体、无差异、漂移和解析错误的页面状态。
- 资源库与 observed 项视觉分离。
- 客户端按钮等宽、亮暗单选。
- 1600px 与 390px 无横向溢出。
- 浏览器无控制台错误、失败请求和异常响应。

### 11.5 完成门禁

```text
pytest 全量
Ruff
compileall
Node 前端测试
node --check
git diff --check
隔离 HOME 端到端浏览器验收
生产服务健康检查
```

实现完成后更新 `docs/operations.md` 和 Agent 工作台相关说明，再进行最终声明。

## 12. 验收标准

1. 页面刷新和服务重启后，资源库内容保持不变。
2. 数据库 Skill 即使尚未安装到任何客户端也会显示在资源库中。
3. 客户端现场 MCP/Skill 只出现在发现状态，显式导入后才进入资源库。
4. 同一 MCP 可保存多个客户端变体，并只投影到选定客户端。
5. 安装、卸载与从库删除是三个独立动作。
6. “按库同步”产生可解释的差异计划或明确的零操作结果。
7. Router 配置、接管状态和故障转移顺序从 SQLite 恢复。
8. Profile 可组合 Provider、MCP、Skill、Prompt 和 Router，并按客户端应用。
9. 所有客户端写入均保留未知字段、生成备份并完成回读验证。
10. 页面不再把扫描结果当作数据库已管理资源。
