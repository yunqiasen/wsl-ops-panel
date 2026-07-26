# Agent 工作台按 CC Switch 精简重构设计

**日期**：2026-07-26
**项目**：WSL Ops Panel
**参考实现**：CC Switch 3.18.0（提交 `878c26f31e012ba32b9772bd080bd4fa9e7d495e`）
**状态**：方案 A 已确认，等待用户复核本文

## 1. 结论

保留 WSL Ops Panel 的 FastAPI Web 面板和现有运维能力，重写 Agent 工作台的信息架构与核心服务。

本次参考 CC Switch 的部分是：

- 单客户端视角的供应商配置管理；
- 任意 API 中转接入；
- 独立本地路由服务与客户端接管；
- 每客户端独立格式的 MCP、Skills、Prompts 管理；
- 紧凑、明确、以真实状态为准的 UI。

本次不把 CC Switch 简化成“只连接 CLIProxyAPI”。CLIProxyAPI 只是可添加的一个中转端点，与 OpenRouter、New API、One API、LiteLLM、自建网关及其他兼容端点地位相同，不是本地路由的依赖。

## 2. 当前基线

2026-07-26 在本地 WSL 现场核对：

- 已检测客户端：Codex、Claude Code、Gemini CLI、OpenClaw；
- 未检测客户端：Claude Desktop、OpenCode、Hermes、Grok Build；
- 已观测 MCP：
  - Codex：`context7`；
  - Claude Code：`Grok Search`、`auggie-mcp`、`context7`、`deepwiki`；
  - Gemini CLI：`context7`；
- 数据库存在 10 个 MCP 定义、6 条真实观测；
- Provider 与 Prompt 当前均没有可验证的完整闭环数据；
- 旧页面已有 MCP 扫描、安装、卸载基础，但仍使用全局设备/客户端选择和过大的卡片布局；
- 现有 Agent 代码及整个仓库存在大量未提交修改，重构必须增量迁移，不做整体回滚或覆盖。

## 3. 产品边界

### 3.1 本阶段目标

1. 只管理当前 WSL 本机，不显示 SSH 设备选择器。
2. 主工作台一次只聚焦一个客户端。
3. 所有客户端状态来自二进制、配置目录和真实配置内容的扫描结果。
4. 每个客户端通过独立适配器读写自己的配置格式。
5. 完成 Provider、路由、MCP、Skills、Prompts 五个真实闭环。
6. 写入操作具备预览、备份、原子替换、回读验证和恢复能力。
7. 密钥不进入页面 HTML、公共 API、任务摘要和普通日志。

### 3.2 明确排除

- Tauri 桌面壳、系统托盘、窗口行为、桌面快捷键；
- 桌面安装器、自动更新、Deep Link；
- Agent 工作台内重复实现开机启动；服务统一由 systemd 管理；
- Dropbox、iCloud、OneDrive、WebDAV、S3 配置同步；
- OAuth 账号中心、订阅余额、多账号池；
- 商业供应商广告和大量合作商预设；
- 会话历史、会话恢复、桌面终端启动器；
- 完整 Token 成本商城和模型定价系统；
- OpenCode OMO、Hermes Memory、OpenClaw Workspace 等客户端专属控制台；
- SSH 多设备矩阵；
- 固定展示未检测客户端或生成演示状态。

### 3.3 后续阶段

- SSH 到 Linux/macOS；
- 自动故障转移、熔断和复杂健康队列；
- 完整请求日志、Token 和费用图表；
- Skills 市场搜索与远程索引；
- Claude Desktop 的 Windows/macOS 实机配置；
- 大规模供应商预设库。

## 4. 信息架构

Agent 工作台改成单客户端视角：

```text
Agent 工作台
├─ 客户端切换条：只显示已检测客户端
├─ 当前客户端状态：版本、配置路径、最近扫描时间、配置健康
└─ 能力页签
   ├─ 供应商
   ├─ 路由
   ├─ MCP
   ├─ Skills
   └─ 提示词
```

规则：

- 客户端切换按钮等宽、紧凑；点击后点亮，再次点击不取消当前客户端。
- 未检测客户端不进入主切换条，只在“支持清单”中显示为未检测。
- 某客户端不具备某能力时，该页签不出现，不渲染无效按钮。
- 页面不再出现全局“设备 × 客户端”矩阵。
- 所有状态均带来源和时间，不使用与真实配置无关的圆点。

## 5. 总体架构

```text
WSL Ops Panel / FastAPI
├─ Agent Web UI
├─ Client Registry
├─ Provider Store + Secret Store
├─ MCP Library
├─ Skill Library
├─ Prompt Library
├─ Client Adapters
└─ Router Control Plane
        │ localhost control API
        ▼
Agent Router（独立 headless 服务）
├─ 应用识别
├─ Provider 热切换
├─ 协议转换
├─ 模型映射
├─ 可选 HTTP/SOCKS 出站代理
└─ 任意上游 API / 中转站
```

FastAPI 是控制面：管理配置、展示状态、执行备份和客户端接管。Agent Router 是数据面：接收客户端 API 请求并转发，不让长连接和流式响应占用面板进程。

## 6. 路由设计

### 6.1 三个概念严格分离

| 概念 | 含义 | 示例 |
|---|---|---|
| Provider / 中转端点 | 实际接收模型请求的上游 API | 官方 API、CLIProxyAPI、OpenRouter、自建中转 |
| 本地路由 | WSL 内运行的统一 API 入口 | `http://127.0.0.1:7888` |
| 出站网络代理 | 本地路由访问上游时经过的 HTTP/SOCKS 代理 | `http://127.0.0.1:7890`、`socks5://127.0.0.1:1080` |

三者互不替代。用户可以：

- 直接让客户端连接任意 Provider；
- 开启本地路由，让客户端先连接 `127.0.0.1:7888`，再由路由转发到任意 Provider；
- 为本地路由配置可选的 HTTP/SOCKS 出站代理。

### 6.2 请求链路

```mermaid
flowchart LR
    A["Claude / Codex / Gemini / 其他客户端"]
    B["本地 Agent Router 127.0.0.1:7888"]
    C["协议转换与模型映射"]
    D["可选 HTTP / SOCKS 出站代理"]
    E["任意官方 API 或中转站"]

    A -->|"开启客户端接管"| B
    B --> C
    C --> D
    D --> E
```

未配置出站网络代理时，`C` 直接访问 `E`。

### 6.3 两种运行模式

#### 直连模式

- 客户端适配器把选中 Provider 的真实端点写入客户端配置。
- 切换 Provider 后，部分客户端需要重启。
- 适合上游协议与客户端协议完全兼容的情况。

#### 本地路由模式

- 客户端配置指向本地 Agent Router。
- Agent Router 按客户端和当前 Provider 选择上游。
- 切换 Provider 只更新路由运行态，不重复改客户端配置。
- 支持协议转换、模型映射和后续故障转移。

### 6.4 应用识别与本地地址

Agent Router 使用同一个监听端口，但为客户端生成明确的专属入口，例如：

```text
Claude Code  → http://127.0.0.1:7888/claude
Codex        → http://127.0.0.1:7888/codex/v1
Gemini CLI   → http://127.0.0.1:7888/gemini
Grok Build   → http://127.0.0.1:7888/grokbuild/v1
```

实际地址由客户端适配器生成。Router 根据路径命名空间识别客户端，不依赖 User-Agent、密钥形状或猜测。未验证路由格式的客户端不开放接管开关。

### 6.5 Router 实现边界

采用独立的 headless 路由服务，而不是把路由硬编码到 CLIProxyAPI，也不把流式代理塞进 FastAPI 页面进程。

路由内核优先抽取和适配 CC Switch MIT 许可下的代理模块，移除 Tauri、窗口、托盘和桌面数据库耦合，保留：

- Anthropic Messages；
- OpenAI Chat Completions；
- OpenAI Responses；
- Gemini 原生路径；
- 流式与非流式转发；
- 请求/响应双向协议转换；
- 模型映射；
- 自定义完整端点；
- 自定义 Header；
- HTTP/SOCKS 出站代理；
- Provider 热切换。

Agent Router 由 systemd user service 管理。默认监听 `127.0.0.1:7888`，修改地址或端口时执行端口校验并重启服务。

### 6.6 路由控制界面

路由页采用与截图相同的核心逻辑，但保持 WSL Panel 的紧凑尺寸：

1. **服务状态**：运行中、已停止、启动失败、端口冲突。
2. **路由总开关**：启动或停止 Agent Router。
3. **首页快捷开关**：决定是否在 Agent 首页显示路由状态与总开关。
4. **客户端接管**：按真实检测结果显示 Claude、Codex、Gemini 等独立开关。
5. **服务地址**：监听地址、端口、复制、重启提示。
6. **出站网络代理**：直连、HTTP、HTTPS、SOCKS5，带连通性测试。
7. **当前路由目标**：每个已接管客户端当前使用的 Provider。

### 6.7 客户端接管事务

开启某客户端路由时：

1. 确认路由服务健康；
2. 读取并解析客户端当前配置；
3. 保存带哈希的原始配置快照；
4. 只修改端点、认证占位和路由所需字段；
5. 原子写入；
6. 重新读取并确认端点已指向本地路由；
7. 将该客户端状态标记为“已接管”。

关闭时：

1. 比较当前文件和接管后快照；
2. 没有外部修改时恢复原始字段；
3. 存在外部修改时做字段级恢复，不整文件覆盖；
4. 回读确认本地路由地址已移除；
5. 保留恢复记录。

停止路由总服务前，如果仍有客户端处于接管状态，界面明确列出并提供“一并恢复后停止”。

## 7. Provider 设计

### 7.1 通用 Provider 模型

每条 Provider 至少包含：

- 名称和所属客户端；
- Base URL 或完整端点；
- 上游协议：Anthropic、OpenAI Chat、OpenAI Responses、Gemini；
- 认证方式和密钥引用；
- 自定义 Header；
- 默认模型；
- 模型映射；
- 是否需要本地路由转换；
- 是否经过出站网络代理；
- 排序和启用状态；
- 来源、更新时间和最近验证结果。

CLIProxyAPI 不设置专属依赖逻辑。它可以作为普通自定义 Provider 保存，也可以提供一个方便填写地址的非商业模板。

### 7.2 Provider 操作

- 导入当前客户端配置；
- 新建自定义 Provider；
- 编辑、复制、排序；
- 测试端点；
- 尝试读取 `/v1/models`；
- 设为当前 Provider；
- 删除本地 Provider 定义；
- 在直连和本地路由模式间切换。

“删除 Provider 定义”与“从客户端配置移除当前 Provider”是两个操作，不互相隐含。

### 7.3 供应商模板

第一阶段只保留：

- 官方格式模板；
- OpenAI Compatible；
- Anthropic Compatible；
- Gemini Compatible；
- CLIProxyAPI 便捷模板；
- 完全自定义。

不复制 CC Switch 的商业广告和大量合作商预设。

## 8. 客户端适配器

统一接口表达能力，不统一配置格式：

```python
class AgentClientAdapter(Protocol):
    def detect(self, home: Path) -> Detection: ...
    def capabilities(self) -> set[str]: ...
    def read_provider(self, home: Path) -> ProviderSnapshot: ...
    def apply_provider(self, home: Path, provider: Provider) -> WriteResult: ...
    def enable_route(self, home: Path, route: RouteAddress) -> WriteResult: ...
    def disable_route(self, home: Path, backup: BackupRef) -> WriteResult: ...
    def read_mcp(self, home: Path) -> McpSnapshot: ...
    def write_mcp(self, home: Path, changes: McpChanges) -> WriteResult: ...
    def skill_paths(self, home: Path) -> list[Path]: ...
    def prompt_targets(self, home: Path) -> list[PromptTarget]: ...
```

要求：

- Codex、Claude Code、Gemini、OpenCode、OpenClaw、Hermes、Grok Build、Claude Desktop 各自拥有适配器定义；
- 主界面只加载真实检测到的适配器；
- 配置路径、格式、认证字段、路由地址写法都在适配器内；
- 任何客户端缺少真实样本或写入验证时只开放扫描，不开放写操作；
- Linux/macOS 路径策略与适配器分离，为以后 SSH 复用保留接口。

## 9. MCP 设计

MCP 页面改成当前客户端的紧凑列表，不再使用多设备矩阵。

明确动作：

- **扫描导入**：读取当前客户端真实 MCP，加入本地库并生成观测；
- **安装/更新**：把选中定义增量写入当前客户端；
- **卸载**：只从当前客户端配置删除指定 MCP；
- **编辑**：编辑当前客户端使用的变体；
- **从本地库删除**：删除逻辑定义，不触碰客户端配置。

状态只有：已安装、需要更新、未安装、配置异常、未观测。每个状态附最近扫描时间和配置哈希。

旧版 `agent_mcp_assignments` 的设备矩阵数据保留用于迁移，不再控制本地页面显示。

## 10. Skills 设计

建立统一 Skill 库，但按客户端独立安装：

- 来源：GitHub URL、本地目录、ZIP；
- 操作：导入、安装、更新、卸载、从库删除；
- 同步方式：软链接或复制；
- 每个客户端适配器声明真实 Skill 目录；
- 安装后检查目标目录、入口文件和来源元数据；
- 客户端没有稳定 Skill 机制时不显示该页签。

Skills 市场搜索和远程仓库索引延后。

## 11. Prompts 设计

Prompt 页面管理真实文件，而不是只保存数据库模板：

- Markdown 编辑；
- 从当前客户端导入；
- 保存模板；
- 应用到当前客户端；
- 停用并恢复接管前内容；
- 删除模板。

目标文件由适配器声明，例如 `AGENTS.md`、`CLAUDE.md`、`GEMINI.md`。写入采用文件级备份、原子替换和回读哈希验证。现有文件内容默认不被空模板覆盖。

## 12. 数据与凭证

### 12.1 数据分层

- 定义：Provider、MCP、Skill、Prompt 的本地库记录；
- 期望：当前用户选择的 Provider、安装项和路由开关；
- 观测：从真实客户端配置读取的状态；
- 操作：预览、备份、写入、验证和恢复结果。

期望状态不冒充观测状态。

### 12.2 凭证规则

- Provider 密钥存放在独立 Secret Store；
- 数据库记录只引用 secret ID；
- API 返回 `has_secret`，不返回原值；
- HTML 不嵌入配置 JSON；
- 日志只记录 Provider ID、目标域名脱敏摘要和结果；
- 导出配置默认移除密钥；
- 本地 Secret Store 和主密钥文件权限为 `0600`。

## 13. API 边界

页面 API 按领域拆分：

```text
/api/agent/clients/*
/api/agent/providers/*
/api/agent/router/*
/api/agent/mcp/*
/api/agent/skills/*
/api/agent/prompts/*
```

面板公开的 Router 管理 API 至少包含：

```text
GET  /api/agent/router/status
POST /api/agent/router/start
POST /api/agent/router/stop
POST /api/agent/router/restart
PUT  /api/agent/router/config
PUT  /api/agent/router/apps/{client_id}/provider
```

`start`、`stop`、`restart` 由 FastAPI 通过 systemd user service 执行。Router 服务停止后本身没有可调用的控制端口。

Router 运行时只在 localhost 暴露内部控制入口：

```text
GET  /health
GET  /status
POST /reload
PUT  /config
PUT  /apps/{client_id}/provider
```

客户端请求的数据面路径与协议适配器分离，至少覆盖：

```text
/v1/messages
/v1/chat/completions
/v1/responses
/v1/models
/v1beta/*
```

## 14. 写入与异常处理

所有客户端写入遵循同一事务框架：

1. 检测客户端与配置路径；
2. 读取并解析；
3. 生成字段级差异预览；
4. 备份原文件与哈希；
5. 写入临时文件；
6. 格式校验；
7. 原子替换；
8. 回读并验证目标字段；
9. 更新观测和操作记录；
10. 失败时恢复备份。

不使用一份通用 JSON 覆盖不同客户端，不因某个写入命令退出码为 0 就显示成功。

路由异常状态必须区分：

- 服务未运行；
- 端口冲突；
- 客户端已接管但服务已停止；
- 上游认证失败；
- 上游协议不匹配；
- 出站代理失败；
- 配置被外部修改；
- 恢复失败。

## 15. UI 视觉约束

- 延续 CC Switch 的紧凑列表、清楚层级和单客户端导航；
- 不复制桌面窗口外壳；
- 客户端按钮、动作按钮保持统一高度；
- 卡片常规高度控制在 64–76px；
- 主要动作直接显示，危险或低频动作放入“更多”；
- 开关只用于持续状态，普通动作使用按钮；
- 空状态只显示真实原因和可执行入口，不填充演示数据；
- 运行状态用文字与颜色共同表达，不单靠圆点。

## 16. 迁移策略

1. 保留现有 Agent 表与文件，不删除用户数据；
2. 先扫描本地客户端，建立新的真实观测；
3. MCP 旧定义迁移到本地库，旧目标矩阵不作为安装事实；
4. Provider 旧记录经过解析校验后迁移，空数据不生成默认记录；
5. Prompt 旧模板保留，首次应用前必须显示目标文件差异；
6. 新页面稳定后再移除旧页面入口和废弃 API；
7. 每一阶段都可切回旧页面读取数据，不对旧数据做破坏性迁移。

## 17. 测试与验收

### 17.1 服务层

- 八个客户端检测结果与真实文件一致；
- Provider 导入、直连写入、路由接管和恢复；
- MCP 每客户端增量安装与精确卸载；
- Skill 复制/软链接安装和卸载；
- Prompt 导入、应用和恢复；
- 凭证脱敏和 API 泄露检查；
- 配置并发修改、格式错误和备份恢复。

### 17.2 Router

- 总开关与 systemd 运行状态一致；
- 每客户端接管独立生效；
- 任意自定义 Base URL 可作为上游；
- CLIProxyAPI 关闭时，其他中转仍可正常使用；
- Anthropic、OpenAI Chat、OpenAI Responses、Gemini 基本协议路径；
- 流式、工具调用、模型映射；
- HTTP/SOCKS 出站代理；
- Provider 热切换无需重写客户端配置；
- 停止服务前恢复所有已接管客户端。

### 17.3 UI

- 主工作台只显示真实检测到的客户端；
- 不支持的页签不出现；
- 1280px、1600px 和 390px 无横向溢出；
- 按钮尺寸统一，卡片紧凑；
- 页面无假圆点、假安装状态和默认演示数据；
- 浏览器控制台无错误，关键请求无异常 HTTP。

### 17.4 完成门禁

```text
实现
→ 自动化测试全部通过
→ 更新对应文档
→ 文档一致性检查
→ 本地服务部署
→ 浏览器与真实客户端配置联合验证
→ 再声明完成
```

## 18. 交付拆分

这是总设计，不合并成一次大改。实施按以下批次独立设计、计划和验收：

1. **基础壳与迁移保护**：新旧页面隔离、客户端注册表、真实检测、数据迁移护栏；
2. **Provider 与 Agent Router**：任意中转、直连、本地路由、接管和恢复；
3. **本地 MCP**：单客户端扫描、安装、更新、卸载和库删除；
4. **Skills**：统一库、客户端目录映射、复制/软链接；
5. **Prompts 与设置**：真实文件编辑、备份恢复和必要设置；
6. **整体 UI 收口**：紧凑布局、浏览器验证、旧入口退役。

每个批次完成测试、文档和本地验证后再进入下一批，避免在当前大量未提交修改上同时重写所有模块。

## 19. 方案取舍

### 采用：FastAPI 控制面 + 独立通用 Agent Router

优点：

- 保留现有项目和数据；
- 支持任意中转；
- 路由生命周期清晰；
- 流式请求不影响面板；
- 后续 Linux/macOS 复用成本低；
- CLIProxyAPI 仍可作为普通 Provider 使用。

### 不采用：只把路由接到 CLIProxyAPI

原因：会把 Provider 管理、协议能力和服务生命周期绑死在一个外部项目上，不符合 CC Switch 的通用中转与本地接管逻辑。

### 不采用：在 FastAPI 主进程内直接实现全部转发

原因：长连接、SSE、协议转换和面板请求共享进程，故障隔离与部署升级都较差。

### 不采用：整体改成 CC Switch/Tauri

原因：会引入大量桌面专属能力，并破坏 WSL Ops Panel 已有的 Web 运维架构。

## 20. 最终验收定义

重构完成后，用户应能在本地 WSL：

1. 选择一个真实安装的 AI 客户端；
2. 导入或新建任意 API 中转 Provider；
3. 选择直连，或启动本地路由并接管该客户端；
4. 在路由模式中即时切换 Provider；
5. 独立安装、卸载该客户端的 MCP 和 Skills；
6. 导入、编辑并应用该客户端的系统提示词；
7. 在任何页面看到的安装、运行和配置状态都能由真实文件或真实进程验证。
