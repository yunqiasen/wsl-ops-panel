# Agent 资源新增、描述与排序设计

## 范围

本轮覆盖 Agent 工作台中所有可重复保存的配置分类：Provider、MCP、Skill、Prompt、Profile。Router 是当前节点唯一运行配置，不属于可新增或排序的资源列表，保持现有编辑方式。

## 目标交互

- 每个分类标题右侧都有明确的“新增 Provider / MCP / Skill / Prompt / Profile”按钮。
- 点击新增后清空对应编辑器、解除 ID 只读、聚焦 ID，并把编辑器标题切换到新增语义。
- 点击列表“编辑”后载入完整资源，ID 锁定，名称、描述和类型专有字段可修改。
- Provider 的描述复用现有 `notes`；MCP 和 Skill 使用现有 `description`；Prompt 新增 `description`；Profile 使用现有 `description`。
- 列表卡片在名称下显示描述。空描述不保留占位高度。
- 保存只更新数据库资源；仍不自动安装或改写客户端原生文件。

## 排序

- Provider 按客户端分别保存顺序；MCP、Skill、Prompt、Profile 保存各自全局顺序。
- SQLite 资源表使用 `sort_index`。旧数据库启动时增量加列，旧数据以现有稳定名称顺序展示，第一次拖动后写入连续序号。
- 新资源默认追加到当前分类末尾，编辑旧资源保留原位置。
- 统一接口 `PUT /api/agent/resources/order` 接收 `resource_type`、完整 `resource_ids`，Provider 额外接收当前 `client_id`。
- 服务端校验 ID 不重复，并要求请求集合与数据库当前集合一致；并发新增/删除导致集合变化时返回冲突，前端重新加载数据库顺序。

## 可访问性与移动端

- 桌面卡片提供拖拽手柄，拖动结束立即保存；拖动中显示占位和目标边框。
- 每张卡片同时提供“上移 / 下移”按钮，支持键盘和触屏，移动后使用同一持久化接口。
- 手柄与移动按钮具有 `aria-label`，按钮禁用状态反映首尾位置。
- 拖拽失败时回读数据库恢复，不保留只在 DOM 中存在的假顺序。

## 数据与兼容

- `agent_mcp_servers`、`agent_skills`、`agent_prompts`、`agent_profiles` 增加 `sort_index INTEGER NOT NULL DEFAULT 0`。
- `agent_prompts` 增加 `description TEXT`。
- `agent_providers` 已有 `sort_index`，补齐查询返回、插入末尾与重排逻辑。
- 现有 Provider Secret、MCP 变体、Skill SSOT、Prompt 变体、Profile items、分配和观察状态不变。

## 验收

- 数据库迁移幂等，描述可往返，五类资源顺序重启后保持。
- API 对五类排序成功，对重复、缺失、越权客户端和集合冲突返回明确错误。
- 五个分类都有新增按钮和描述字段；编辑锁定 ID；列表显示描述。
- 拖拽及上移/下移都调用持久化接口，刷新后顺序不回弹。
- 390px 无横向溢出，触屏替代按钮尺寸满足操作要求。
