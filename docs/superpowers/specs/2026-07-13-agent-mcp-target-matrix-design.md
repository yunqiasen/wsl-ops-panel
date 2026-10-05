# Agent MCP 多设备目标矩阵设计

**日期**：2026-07-13
**项目**：WSL Ops Panel / WSL 维护更新管理面板
**状态**：方案 B 已落地并完成验收

## 1. 背景

当前 Agent/MCP 页面把 `agent_mcp_targets` 中的 `apps` 标记渲染成客户端圆点。该字段表示本地数据库中的默认目标，不表示设备上的真实安装状态，也没有设备维度、扫描时间和同步结果。

2026-07-13 现场读取当前 WSL 客户端配置，仅按 MCP 名称核对：

| 客户端 | 当前真实 MCP |
|---|---|
| Codex | `context7` |
| Claude Code | `auggie-mcp`、`context7`、`deepwiki`、`Grok Search` |
| Gemini CLI | `context7` |
| OpenCode | 无 |
| OpenClaw | 无 |
| Hermes | 无 |

数据库标记与真实配置存在大量差异，不能继续用圆点表示“已安装”。当前 `import-local` 只读取面板所在 WSL 的 `Path.home()`；当前同步使用一份统一 spec 写入所有目标客户端，并替换目标客户端的完整 MCP 表。这无法可靠支持多设备、多客户端和不同格式。

## 2. 目标

1. 页面只展示有来源、有时间、有目标维度的真实状态。
2. 设备与客户端选择统一为等宽切换按钮，不显示 checkbox。
3. 支持在一次安装操作中为不同设备、不同客户端分配不同 MCP。
4. 每个客户端通过独立适配器解析和写入自己的配置格式。
5. 安装默认增量合并，保留未选中的已有 MCP。
6. 写入前预览差异并备份，写入后重新读取验证。
7. 离线、未检测、未支持、漂移和失败状态必须明确展示。
8. 任何凭证不得出现在页面、日志、任务摘要、API 公共响应或文档中。

## 3. 非目标

- 本阶段不实现定时自动同步。
- 本阶段不实现设备组或组织级策略。
- 本阶段不实现配置版本市场。
- 本阶段不自动安装 Codex、Claude Code、Gemini CLI 等 CLI 本体。
- Windows MCP 写入在专用适配器完成前保持显式“不支持”，不能伪装成功。

## 4. 核心概念

### 4.1 MCP 定义

描述一个逻辑 MCP：ID、显示名称、描述、主页、文档和标签。定义本身不等于已安装。

### 4.2 MCP 变体

同一逻辑 MCP 针对客户端和平台保存专属配置。唯一范围：

```text
mcp_id + client_id + platform
```

变体保存规范化配置及客户端专属覆盖。凭证保留在受限数据库中，公共响应只返回脱敏结果。

### 4.3 期望分配

表示用户希望在哪个设备、哪个客户端安装哪个 MCP 变体：

```text
node_id + client_id + mcp_id
```

它是期望状态，不是实际状态。

### 4.4 实际观测

表示最近一次从目标设备真实读取到的结果，包含：存在状态、脱敏配置摘要、配置哈希、扫描时间和错误。

### 4.5 操作记录

记录预览、备份、写入、验证的逐目标结果。任务成功必须以回读验证为准，不能只以写入命令退出码为准。

## 5. 数据模型

保留 `agent_mcp_servers` 作为逻辑 MCP 定义。新增四张表。

### 5.1 `agent_mcp_variants`

| 字段 | 说明 |
|---|---|
| `mcp_id` | MCP 定义 ID |
| `client_id` | `codex`、`claude`、`gemini`、`opencode`、`openclaw`、`hermes` |
| `platform` | `linux`、`macos`、`windows`、`any` |
| `spec_json` | 规范化配置或客户端专属配置 |
| `source` | `manual`、`scan`、`migration` |
| `created_at` / `updated_at` | 时间 |

唯一键：`(mcp_id, client_id, platform)`。

### 5.2 `agent_mcp_assignments`

| 字段 | 说明 |
|---|---|
| `node_id` | `__local__` 或 SSH 节点 ID |
| `client_id` | AI 客户端 |
| `mcp_id` | MCP 定义 ID |
| `variant_client_id` / `variant_platform` | 使用的变体 |
| `desired_enabled` | 是否期望安装 |
| `updated_at` | 最后修改时间 |

唯一键：`(node_id, client_id, mcp_id)`。

### 5.3 `agent_mcp_observations`

| 字段 | 说明 |
|---|---|
| `node_id` | 设备 |
| `client_id` | 客户端 |
| `mcp_id` | 扫描到的 MCP ID |
| `present` | 是否存在 |
| `spec_hash` | 规范化后配置哈希 |
| `public_spec_json` | 脱敏配置摘要 |
| `status` | `installed`、`drifted`、`missing`、`unknown`、`error` |
| `scanned_at` | 扫描时间 |
| `error` | 无敏感信息的失败原因 |

唯一键：`(node_id, client_id, mcp_id)`。

### 5.4 `agent_mcp_operations`

保存每个目标的任务 ID、动作、阶段、备份位置摘要、验证结果和错误。不得保存原始凭证或完整配置载荷。

### 5.5 旧数据迁移

- 保留 `agent_mcp_servers` 的定义与 spec，不删除用户数据。
- `agent_mcp_targets` 只作为旧版期望信息读取，不再作为实际状态展示。
- 旧 `apps=true` 可迁移为 `source=migration` 的通用变体建议，但不自动创建“已安装”观测。
- 首次扫描后才产生真实状态。

## 6. 客户端适配器

定义统一接口：

```python
class McpClientAdapter(Protocol):
    def detect(self, home: Path) -> bool: ...
    def read(self, home: Path) -> dict[str, dict]: ...
    def normalize(self, raw: dict) -> dict: ...
    def render(self, normalized: dict) -> dict | str: ...
    def merge(self, existing: object, selected: dict[str, dict]) -> object: ...
    def remove(self, existing: object, server_ids: set[str]) -> object: ...
    def validate(self, rendered: object) -> None: ...
```

适配器职责：

| 客户端 | 当前配置载体 | 写入策略 |
|---|---|---|
| Codex | TOML `mcp_servers` | 只增量更新选中的 server table |
| Claude Code | JSON `mcpServers` | 保留其他顶层字段和未选中的 MCP |
| Gemini CLI | JSON `mcpServers` | 保留其他顶层字段和未选中的 MCP |
| OpenCode | JSON `mcp` | 转换 `local/remote`、command 数组和 environment |
| OpenClaw | JSON | 扫描实际存在的 MCP 键后增量更新 |
| Hermes | YAML `mcp_servers` | 保留 model、temperature 等其他字段 |

平台层处理命令、路径和 Shell 差异。Windows 未实现前，预览阶段即返回明确的 `unsupported`，不进入任务队列。

## 7. API

### 7.1 扫描

```http
POST /api/agent/mcp/scan
{
  "node_ids": ["__local__", "mac-book"],
  "apps": ["codex", "claude"]
}
```

作用：从每个目标真实读取客户端配置，更新 observations。远程设备复用 SSH 节点。响应返回逐目标任务或跳过原因。

### 7.2 状态查询

```http
GET /api/agent/mcp/inventory?node_ids=__local__&apps=codex,claude
```

返回定义、变体、期望分配和最近观测。公共数据必须脱敏。

### 7.3 保存定义和变体

```http
POST /api/agent/mcp/servers
POST /api/agent/mcp/variants
```

保存逻辑定义与客户端专属配置。不能用顶部选择器隐式修改所有客户端。

### 7.4 预览

```http
POST /api/agent/mcp/preview
{
  "assignments": [
    {"node_id": "__local__", "client_id": "codex", "mcp_ids": ["context7"]},
    {"node_id": "mac-book", "client_id": "claude", "mcp_ids": ["deepwiki"]}
  ]
}
```

返回每个目标的新增、更新、保留、移除、未支持和离线状态。安装预览不得隐式移除任何 MCP。

### 7.5 应用

```http
POST /api/agent/mcp/apply
```

请求携带经过预览的 assignments 和预览版本/hash，避免预览后配置发生变化。每个设备拆成独立串行任务，每个客户端在同一设备内按稳定顺序处理。

### 7.6 卸载

```http
POST /api/agent/mcp/uninstall
```

卸载是独立动作，必须明确选择目标。安装/更新接口不能通过“未选中”推断删除。

## 8. 写入与验证流程

每个设备和客户端执行：

1. 检查设备在线与平台支持。
2. 读取当前配置并规范化。
3. 比较预览 hash，防止过期预览覆盖新变化。
4. 生成增量合并结果。
5. 校验 TOML/JSON/YAML 结构。
6. 在目标设备创建权限受限的备份。
7. 原子写入临时文件并替换目标文件。
8. 重新读取目标配置。
9. 规范化并比较预期 hash。
10. 更新 observation 和 operation 状态。

回读不一致时任务状态为失败，并保留备份供明确回滚。不能自动覆盖第二次。

## 9. UI 信息架构

### 9.1 顶部范围选择

保留两个独立区域：设备、AI 客户端。统一使用 `.agent-scope-button`：

- 桌面尺寸：宽 `168px`、高 `48px`。
- 小屏尺寸：两列等宽，高度不变。
- 元素使用 `<button type="button" aria-pressed="true|false">`。
- 不显示 checkbox。
- 选中：蓝色边框、浅蓝背景、清晰文字。
- 未选：中性边框、白色背景。
- 离线或不支持：显示状态文字和原因；禁用态不能只靠透明度。
- 动画只使用颜色和阴影，时长 150–200ms，不改变组件尺寸。

设备和客户端按钮只控制当前查看范围。具体安装目标由安装矩阵决定。

### 9.2 MCP 状态列表

删除六个客户端字母圆点。每个 MCP 行显示：

- MCP 名称和 ID。
- 当前范围摘要，例如 `当前 WSL · Codex`。
- 真实状态徽章：`已安装`、`未安装`、`配置漂移`、`未扫描`、`离线`、`不支持`。
- 最近扫描时间。
- 操作：`安装到…`、`编辑`、`从目标卸载…`、`从本地库删除`。

本地库删除和目标卸载必须保持不同语义。

### 9.3 安装目标矩阵

点击 `安装 / 更新 MCP` 或行内 `安装到…` 打开矩阵：

- 行：设备。
- 列：客户端。
- 单元格：可切换目标，显示 `已检测`、`未检测`、`离线`、`不支持`。
- 单元格使用按钮语义和 `aria-pressed`，支持键盘操作。
- 多选 MCP 时，矩阵表示这一批 MCP 的目标集合。
- 下一步必须进入差异预览，不能直接写入。

### 9.4 扫描并导入

将 `导入已有` 改为 `扫描并导入`：

1. 使用顶部范围选择作为扫描来源。
2. 显示每个设备/客户端发现的 MCP 数量和名称。
3. 同名同配置合并为一个逻辑定义。
4. 同名不同配置保存为客户端/平台变体，不通过修改 ID 制造伪副本。
5. 用户确认后写入本地库。

### 9.5 结果反馈

应用结果按目标显示：

```text
当前 WSL / Codex：成功，已回读验证
MAC-Book / Claude Code：离线，未执行
Win / Gemini CLI：暂不支持 Windows 写入
```

前端必须展示 API 的 `skipped` 和逐目标错误，不能只显示 `queued_count`。

## 10. 状态定义

| 状态 | 条件 |
|---|---|
| `installed` | 真实观测存在，且 hash 与期望变体一致 |
| `drifted` | 真实观测存在，但 hash 不一致 |
| `missing` | 有期望分配，但真实观测不存在 |
| `not_assigned` | 无期望分配，真实观测也不存在 |
| `unmanaged` | 真实设备存在，但本地库没有对应定义或分配 |
| `unknown` | 从未扫描或扫描结果已失效 |
| `offline` | 设备离线，无法扫描 |
| `unsupported` | 当前平台/客户端没有安全适配器 |
| `error` | 扫描、解析、写入或回读失败 |

颜色不能作为唯一信息，所有状态都显示文字。

## 11. 安全要求

- 页面和公共 API 只返回脱敏 spec。
- 凭证不能进入 HTML 内嵌 JSON。
- 凭证不能进入命令行参数、任务摘要或系统终端日志。
- 任务计划只保存受保护引用或权限受限载荷文件，不保存可逆 Base64 凭证命令。
- 备份、临时文件和凭证文件权限为 `0600`，目录为 `0700`。
- 扫描和回读日志只输出 MCP ID、目标、阶段和脱敏错误。

## 12. 测试策略

### 12.1 服务层

- 每个适配器读取、规范化、增量合并和保留无关字段。
- 同名不同客户端配置生成独立 variant。
- 安装不删除未选中的 MCP。
- 卸载只删除明确指定的 MCP。
- 预览 hash 过期时拒绝写入。
- 写入后回读不一致时任务失败。

### 12.2 数据层

- 定义、变体、分配、观测和操作记录的唯一约束。
- 旧 apps 数据迁移不产生虚假 installed 状态。
- 公共查询不返回凭证。

### 12.3 API

- 扫描支持本机与在线 SSH 节点。
- 离线、未知节点、无效客户端和不支持平台返回明确结果。
- preview/apply 请求支持不同设备使用不同客户端和 MCP 集合。
- apply 拒绝过期预览。

### 12.4 页面与交互

- 设备和客户端使用 `button[aria-pressed]`，没有可见 checkbox。
- 两组按钮尺寸一致。
- MCP 行不再渲染 `server.apps` 圆点。
- 状态来自 observations。
- 安装矩阵可键盘操作。
- API 跳过和失败原因可见。

### 12.5 浏览器验收

- 桌面宽度：1600px、1280px。
- 移动宽度：390px。
- 无横向溢出。
- 等宽按钮在移动端变为两列。
- 目标矩阵在小屏使用按设备分组的卡片，不依赖横向滚动。
- 无控制台错误和异常 HTTP 响应。

## 13. 验收标准

1. 当前 WSL 扫描结果与第 1 节真实 MCP 名称一致。
2. 数据库旧 apps 标记不再显示为已安装。
3. 设备与客户端没有可见 checkbox，按钮等宽、可切换、键盘可用。
4. 可在一个预览中表达：当前 WSL/Codex 安装 `context7`，MAC-Book/Claude 安装 `deepwiki`。
5. 每个客户端使用自己的适配器和配置格式。
6. 安装保留未选中的已有 MCP。
7. 卸载只能通过独立明确动作执行。
8. 应用成功必须有目标配置回读验证。
9. 离线、不支持和失败原因在页面可见。
10. 敏感信息扫描无泄漏。
11. 定向测试、完整测试、Ruff、compileall、JavaScript 语法检查全部通过。
12. 更新 README 或运维/API 文档后，重启服务并完成本机、Tailscale 和浏览器验收。
