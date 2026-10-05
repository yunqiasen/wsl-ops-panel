# Agent Provider CC Switch 逻辑重构设计

## 目标

把当前 Provider 从“统一 routing 表单 + 客户端配置快照”重构为 CC Switch 的真实模型：数据库按客户端保存可编辑 Provider，客户端原生文件只是导入来源和运行投影；直连和 Router 接管共用同一 Provider 资源，但使用不同启用路径。

本轮只处理本地 WSL。SSH 节点继续复用后续同一适配层，不进入本轮 UI。

## 已确认根因

- 当前导入为统一的 `local-current / 当前本机配置`。
- Claude/Codex 生产记录均没有 `settings.routing`。
- Provider 编辑器只从 `settings.routing` 回填 Base URL、模型、协议和凭证，因此导入后显示为空。
- 原生快照、数据库 Provider、Router 上游和客户端当前配置被混入同一层，导致“已保存”“当前”“已接管”语义互相覆盖。

## 数据模型

### Provider 主记录

沿用 `agent_providers`，增加 `meta_json`：

- `settings_json`：客户端原生 Provider 配置，是 Provider 配置的数据库事实源。
- `meta_json`：不属于客户端原生文件的管理元数据，包括 API 格式、完整 URL、模型映射、Router 出站代理、只读来源、官方模式、导入类型和客户端投影模式。
- `is_current`：独占模式客户端当前启用的数据库 Provider。
- `source`：`manual`、`import`、`seed`。

列表不返回完整 `settings_json`。详情返回脱敏后的原生配置、`meta` 和服务端计算的 `summary`。

### 服务端摘要

新增客户端适配器统一计算：

- `base_url`
- `model` / `models`
- `api_format`
- `auth_mode`
- `has_credentials`
- `editable`
- `read_only_reason`
- `native_mode`
- `live_state`

摘要只用于展示和 Router 运行配置，不反向覆盖原生配置。

## 客户端模式

### 独占模式

Claude Code、Codex、Gemini CLI、Grok Build：

- 同一客户端只有一个当前 Provider。
- “启用”在 Router 未接管时写入客户端原生文件并设为当前。
- Router 已接管时只切换 Router 上游，不用第三方配置覆盖接管文件。
- 编辑当前 Provider 时先读取 live 配置回填，再与数据库私有字段合并。

### 累加模式

OpenCode、OpenClaw、Hermes：

- 数据库可保存多个 Provider，客户端文件中也可同时存在多个 Provider。
- 主按钮显示“添加”“移除”或“已添加”。
- OpenClaw/Hermes 另有“设为默认/启用”状态。
- 导入时解析客户端文件中的每一个 Provider，按原生 Provider ID 分别入库。
- Hermes `providers` 字典来源保持原生只读；`custom_providers` 可编辑。

## 导入与迁移

- 旧 `local-current` 自动迁移为 `default`，保留排序、当前状态、描述和配置。
- Claude/Codex/Gemini/Grok Build 在该客户端没有用户 Provider 时，把当前 live 配置导入为 `default`；已有用户 Provider 时不覆盖。
- OpenCode/OpenClaw/Hermes 每次导入只新增未入库的原生 Provider，不覆盖用户编辑过的数据库资源。
- 官方登录态保存为明确的官方 Provider/官方模式，不再伪装成普通空配置。
- 导入时剥离 MCP、Skill、Prompt 等其他资源投影，避免 Provider 切换复活已删除资源。

## 编辑器

编辑器使用公共基础字段和客户端专属字段：

- 公共：ID、名称、描述、官网、状态摘要。
- Claude：端点、认证字段、API Key、主模型及三档默认模型。
- Codex：端点、API Key、模型、模型 Provider、Responses/Chat 协议。
- Gemini：端点、API Key、模型。
- Grok Build：Profile、端点、API Key/环境变量、模型、后端协议。
- OpenCode：Provider Key、端点、API Key、SDK/API 类型、模型。
- OpenClaw：端点、API Key、API 类型、模型列表。
- Hermes：端点、API Key/环境变量、API 模式、模型列表；只读来源禁用保存。
- 高级区保留完整脱敏原生 JSON/TOML/YAML，便于处理客户端扩展字段。

保存只保存数据库。是否写客户端由卡片主按钮决定，取消“编辑器保存顺便设当前”的混合逻辑。

## Provider 卡片

采用紧凑运维卡片：

- 第一行：图标、名称、当前/已添加/只读/官方状态。
- 第二行：Base URL，超长截断并可查看完整值。
- 第三行：模型、协议、凭证状态、来源。
- 操作：启用/添加/移除、编辑、复制、检测、删除。
- 当前独占 Provider 不允许直接删除；累加模式删除前先移除客户端投影。
- 列表排序继续按客户端独立持久化。

## Router 边界

- Provider 数据库不绑定 CLIProxyAPI，任何 HTTP(S) 上游都可保存。
- Router 使用适配器从原生 Provider + `meta_json` 生成标准运行 Profile。
- Router 的重试、故障转移、模型映射、出站代理不写入客户端原生 Provider 配置。
- Provider 卡片“启用”在接管态调用 Router 热切换；直连态调用客户端投影。

## API

- `GET /api/agent/providers/{app_id}/{provider_id}`：返回脱敏原生配置、meta、summary。
- `POST /api/agent/providers`：按客户端适配器校验并保存原生配置。
- `POST /api/agent/providers/import-local`：按独占/累加模式导入。
- `POST /api/agent/providers/{app_id}/{provider_id}/activate`：统一处理直连启用、Router 热切换、累加添加。
- `POST /api/agent/providers/{app_id}/{provider_id}/duplicate`：完整复制配置和描述，新 ID 追加排序末尾。
- `DELETE /api/agent/providers/{app_id}/{provider_id}`：按模式检查当前状态和客户端投影。

旧 `/apply`、`/current` 在过渡期保留兼容，前端不再使用。

## 验收

- 生产现有 Claude/Codex `local-current` 无损迁移为 `default`。
- 导入后卡片和编辑器能显示真实 Base URL、模型、协议和凭证状态。
- 七个客户端的新增、编辑、复制、检测、启用/添加、移除、删除语义符合各自模式。
- 当前 Provider 编辑可回填 live 修改，不丢数据库私有字段。
- Router 接管态切换不覆盖客户端接管配置；关闭接管后直连启用可正确写回。
- API 和页面不输出真实凭证。
- 1600px 与 390px 无横向溢出，控制台、失败请求和错误响应为空。
