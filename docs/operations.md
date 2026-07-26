# WSL Ops Panel Phase 1 运维说明

## 1. 服务安装与启动

### 1.1 安装 systemd 服务

```bash
bash scripts/install_systemd_service.sh
bash scripts/install_agent_router_service.sh
```

安装后服务名：

- `wsl-ops-panel.service`
- `wsl-agent-router.service`

默认监听：

- `127.0.0.1:8328`
- `http://<Tailscale-IP>:8328`

### 1.2 检查服务状态

```bash
systemctl status wsl-ops-panel.service --no-pager
systemctl status wsl-agent-router.service --no-pager
curl -I http://127.0.0.1:8328/healthz
curl -fsS http://127.0.0.1:7888/health
```

## 2. sudo 权限要求

Phase 1 需要通过单独的 sudoers include 提供 passwordless sudo。

建议最少白名单覆盖：

- `docker`
- `systemctl`
- `journalctl`

当前仓库自带预检脚本：

```bash
bash scripts/check_sudo_rules.sh
```

该脚本会：

- 从 `config/objects/*.yaml` 读取已注册的 systemd `unit_name`
- 检查 `systemctl disable --now <unit>` 的 sudo 权限
- 检查 `docker ps` 的 sudo 权限

## 3. Phase 1 支持范围

### 3.1 Docker

Phase 1 支持：

- 更新最新版
- 指定版本部署
- 开启、关闭、重启
- 自启动开关
- 删除
- 完全删除

说明：

- `start` 使用 `docker compose up -d`，启动 Compose 文件中的完整应用栈。
- `stop` 和 `restart` 不指定单个服务，数据库、缓存、反代等内置服务与主程序同步处理。
- 非标准 Compose 文件、主容器和业务入口由 `config/objects/*.yaml` 显式锁定，不再按容器名称猜测。
- Docker 在 Phase 1 支持 `full_delete`
- full delete 只对已注册对象生效
- 提交后的动作会进入全局串行后台队列自动执行
- stdout / stderr 会同时写入任务日志和系统终端

### 3.2 systemd

Phase 1 支持：

- 删除

说明：

- systemd 删除映射为：`sudo systemctl disable --now <unit>`
- systemd 在 Phase 1 **不支持** `full_delete`
- 提交后的动作会进入全局串行后台队列自动执行
- `update_latest` / `deploy_version` 暂未实现

### 3.3 只读分类

以下分类当前仅展示，不提供通用资产动作：

- 宿主机进程
- 系统基础设施
- agent cli

Agent 分类使用独立工作台，不走通用资产动作。

### 3.4 Node

- 默认管理 `npm list -g` 扫到的普通全局包
- 支持：`update_latest / deploy_version / delete / full_delete`
- `@openai/codex`、`@anthropic-ai/claude-code`、`@google/gemini-cli`、`@jackwener/opencli`、`@qingchencloud/openclaw-zh` 当前受保护

### 3.5 Python

- 当前只管理 `python3 -m pip list --format=json` 对应的 Miniconda base
- 仅 `openai / fastapi / uvicorn / playwright` 开放执行动作
- 支持：`update_latest / deploy_version / delete / full_delete`

### 3.6 Docker strategy notes

- `compose_pull`：普通镜像型项目，版本列表来自镜像仓库 tags
- `compose_local_build_git_tag`：本地构建型项目，版本列表来自 Git tags
- `openai-cpa`：使用 panel 托管 override，不直接信任上游 compose 的运行差异

Project 扫描说明：

- 扫描 `/home/div/1_Project_dir`、`AI`、`Project` 及显式维护项目。
- 同时读取系统级 systemd 与 `~/.config/systemd/user`。
- 同一 Project 关联多个 user-systemd 服务时，启停和重启会一次处理整组服务。
- 只有检测到 systemd 单元或明确启动入口的 Project 才显示运行控制；纯源码工具不提供无效启停按钮。
- systemd 显示 inactive 但声明端口仍在监听时，卡片按实际运行状态显示。

### 3.7 分类页搜索与运行状态筛选

- Docker、Project、systemd、宿主机进程分类支持“运行中/已关闭”筛选。
- 两个状态按钮互斥；再次点击当前按钮恢复全部。
- 文字搜索与状态筛选同时生效，右侧数量为最终可见资产数。
- 无法确认运行态的资产只在默认视图显示，不会被归入“已关闭”。
- 宿主机进程页只扫描当前监听项，不保留已关闭进程历史。

### 3.8 分类卡片对齐

- 分类卡片按“标题、简介、状态操作、指标、业务端点、技术信息”建立稳定纵向槽位，同排卡片的主要内容起点保持一致。
- Docker 与 Project 卡片会为可选的版本状态、微信模板和最多三条业务端点预留空间；缺少数据时不显示假内容。
- 页面视口不超过 560px 时取消预留高度并恢复自然流，避免手机窄屏出现大块空白。

### 3.9 Agent 工作台

Agent 页只管理当前 WSL。没有设备栏和目标矩阵；已检测客户端使用等宽互斥按钮，一次只操作一个客户端。检测依据是客户端配置路径或真实二进制，不使用预置圆点。

客户端适配器覆盖 Claude Code、Claude Desktop、Codex、Gemini CLI、Grok Build、OpenCode、OpenClaw、Hermes。每个适配器独立声明配置路径、格式和可写能力；缺少稳定写入验证的客户端只显示已确认能力。

#### Provider 与 Route

Provider 可填写任意 HTTP(S) `Base URL`、API 格式、认证方式、模型、模型映射和自定义 Header。官方 API、自建中转、OpenRouter、CLIProxyAPI 地位相同。凭证写入 `data/agent/provider-secrets.json`，权限 `0600`；页面、公开 API 和日志不返回原值。

两种请求链路：

1. 直连：客户端直接写入选中的 Provider。
2. 本地路由：客户端 → `127.0.0.1:7888/<client>/...` → 协议转换/模型映射 → 任意 Provider。

“出站网络代理”只控制 Router 到上游的 HTTP/HTTPS/SOCKS5 网络出口，不等于 Provider 中转。

操作顺序：

1. 新建或导入 Provider，填写任意上游。
2. 点击 Provider 的“当前”，把该 Provider 的完整运行配置写入 Router 私有文件。
3. 启动 `wsl-agent-router.service`。
4. 对目标客户端开启“接管”。系统先备份客户端原配置，再写本地 Router 地址并回读。
5. 关闭接管时恢复原配置；文件被外部修改时只处理 Router 管理字段。
6. Router 停止前存在接管项时，页面会先恢复客户端再停止。

Router 私有配置：`data/agent/router.json`，权限 `0600`。默认只监听 `127.0.0.1:7888`。

#### MCP

MCP 状态只来自当前客户端真实配置扫描：

- Codex：`~/.codex/config.toml`
- Claude Code：`~/.claude.json`
- Gemini CLI：`~/.gemini/settings.json`
- OpenCode：`~/.config/opencode/opencode.json`
- OpenClaw：`~/.openclaw/openclaw.json`
- Hermes：`~/.hermes/config.yaml`

明确动作：

- “扫描当前配置”：读取当前客户端并更新观测。
- “安装/更新”：只把选中本地库定义增量写入当前客户端。
- “卸载”：只删除明确选中的 MCP，其他 MCP 和顶层配置保留。
- “编辑”：修改本地库定义。
- “删库”：只删除面板逻辑定义，不触碰客户端。

每次写入前备份，按客户端格式校验，临时文件原子替换，最后重新扫描确认。

#### Skills

来源支持本地目录、Git URL、ZIP。同步模式：

- `copy`：复制成当前客户端独立副本。
- `symlink`：本地目录软链接，源目录变更立即可见。

安装要求存在 `SKILL.md`；ZIP 禁止绝对路径、`..` 和符号链接条目。安装元数据写入 `.wsl-ops-skill.json`。卸载不会直接销毁，目标移到 `~/.wsl-ops-agent-backups/skills/`。

#### Prompts

“导入当前内容”读取适配器声明的真实文件，例如 Codex `~/.codex/AGENTS.md`、Claude `~/.claude/CLAUDE.md`、Gemini `~/.gemini/GEMINI.md`。应用模板时：

1. 保存接管前文件和哈希。
2. 原子写入新内容。
3. 回读哈希验证。
4. “恢复原文件”还原接管前内容。

若应用后文件又被其他程序修改，恢复操作返回冲突并保留当前文件。备份和状态位于 `data/agent/prompt-files/`，权限 `0600`。

## 4. 常用命令

### 4.1 全量测试

```bash
pytest -q
```

### 4.2 代码检查

```bash
ruff check
```

### 4.3 本地开发启动

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8328 --reload
```

## 5. 重要路径

- 项目目录：`/home/div/1_Project_dir/AI/wsl-ops-panel`
- 面板 systemd unit：`/etc/systemd/system/wsl-ops-panel.service`
- Router systemd unit：`/etc/systemd/system/wsl-agent-router.service`
- Router 私有配置：`data/agent/router.json`
- Prompt 备份状态：`data/agent/prompt-files/`
- Skill 卸载备份：`~/.wsl-ops-agent-backups/skills/`
- SQLite：`data/tasks.sqlite3`
- 操作日志：`data/operations/`
- 终端日志：`data/terminals/`
