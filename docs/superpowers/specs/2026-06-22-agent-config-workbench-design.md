# Agent Config Workbench Design

## 目标

把 Agent 分类从“只读扫描卡片”改成 AI 客户端配置工作台。第一阶段落地 MCP、Prompts、Skills 三个入口，参考 cc-switch 的统一管理思路，但只做适合 WSL Ops Panel 的 Web + SSH 场景。

## 范围

第一阶段做可用闭环：

- 客户端总览：Codex、Claude Code、Gemini CLI、OpenCode、OpenClaw、Hermes。
- MCP：统一服务器列表，支持从本机配置导入，编辑 JSON spec，勾选同步到 Codex / Claude / Gemini，写入前备份。
- Prompts：提示词模板列表，支持选择客户端并写入对应 prompt 文件，写入前备份。
- Skills：先做扫描展示和目录说明，不做安装/更新/删除。
- 远程设备：沿用 SSH 节点选择；同步动作进入任务队列。

不做：会话历史、供应商切换、代理服务、用量统计、桌面托盘。

## 架构

新增 Agent 专用服务层，不把逻辑塞进通用 category 模板。

- `app/services/agent_clients.py`：客户端定义、路径、支持能力。
- `app/services/agent_mcp.py`：统一 MCP store、导入、写入命令生成。
- `app/services/agent_prompts.py`：提示词 store、目标 prompt 文件、写入命令生成。
- `app/services/agent_skills.py`：本地 skill 目录扫描。
- `app/api/agent.py`：Agent 页面 API 和任务入队。
- `app/templates/agent_category.html`：Agent 工作台页面。

数据放在 `data/agent/`：

- `mcp_servers.json`
- `prompts.json`
- `backups/`

## 写入安全

- 不整文件覆盖。
- Codex 只改 `[mcp_servers]`。
- Claude / Gemini 只改 `mcpServers`。
- Prompt 文件写入前备份。
- 所有写入都通过任务队列执行。
- 远程设备使用 SSH 执行同类命令；不兼容的客户端先跳过并记录日志。

## UI

Agent 页面分四块：

1. 客户端状态条。
2. MCP 管理：导入本机、添加/编辑、勾选客户端、同步到设备。
3. Prompts：新建模板、选择客户端、应用到设备。
4. Skills：展示各客户端 skill 目录和数量。

## 测试

- 服务层：导入 MCP、生成写入命令、Prompt 写入命令、Skill 扫描。
- 页面：Agent 分类渲染专用工作台。
- API：导入本机 MCP、MCP 同步入队、Prompt 应用入队。
