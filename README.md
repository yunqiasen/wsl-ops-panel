# WSL Ops Panel

WSL 维护更新管理面板。

一个面向 WSL 环境的运维控制面板。导航包含 8 个分类和任务、终端、设置 3 个控制中心；职责分为以下四组（不改变导航）：

| 分组 | 模块 | 负责范围 | 不包含 |
| --- | --- | --- | --- |
| 运行对象 | Docker | Compose 项目、关联容器、镜像/源码部署、启停与删除 | Docker Engine 升级 |
| 运行对象 | Project | 非 Compose 源码目录、Git、关联 system/user systemd | 任意启动脚本托管；Git 拉取不等于构建部署 |
| 工具与配置 | Node | npm 全局包、安装目录、包版本与动作策略 | 解释器升级、Agent 原生配置 |
| 工具与配置 | Python | 当前目标环境的 pip 包；更新/删除遵守面板白名单 | 多环境自动管理、系统 Python 升级 |
| 工具与配置 | Agent | 当前 WSL 的 Provider、Route、MCP、Skills、Prompts、Profiles 与原生配置 | CLI 本体安装、远程 Agent 配置管理 |
| 设备与环境 | SSH 与配置同步 | 设备连接、通用配置状态扫描、草稿及明确的追加写入 | 重新定义包策略；草稿不会自动应用 |
| 设备与环境 | 宿主机进程 | TCP 监听观察、真实所属对象识别及受限操作 | 全部后台进程、UDP、停止项历史 |
| 设备与环境 | 系统基础设施 | 工具版本与有限维护；APT 索引刷新、cloudflared/bun/tailscale 更新 | APT 全系统升级、任意系统组件更新 |
| 面板控制 | 任务中心 | 已入队操作的串行执行、状态、日志与诊断 | 手动终端命令、所有同步操作的统一总账 |
| 面板控制 | 终端中心 | 系统日志、交互 PTY、断线回看与上传 | 面板重启后恢复原 PTY 进程 |
| 面板控制 | 设置 | 注册表摘要；重载分类、对象、执行规则与包策略 | 代码热加载、面板重启、改写已入队计划 |

- `systemd`、`agent_cli` 是停用的兼容分类，不新增导航；CLI 本体按安装器回到包管理，客户端原生配置仍由 Agent 负责。
- `/logs` 转向终端中心；Cloudflare 访问与通知属于 Docker/Project 的附属功能。
- Python 初期设计是 Miniconda base 白名单。安装目录支持新增包及扩展安装器，但并未自动建立多个环境的统一管理；以实际执行目标为准。
- 分类、子分组与本轮修复证据见 [全模块修复记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/superpowers/plans/2026-10-06-all-module-repair.md)。

## 基本信息

- 项目目录：`/home/div/1_Project_dir/AI/wsl-ops-panel`
- 面板端口：`8328`
- 本地 Agent Router 端口：`7888`
- 应用入口：`app.main:app`
- Python 要求：`>=3.13`
- 默认服务地址：`http://127.0.0.1:8328`
- Tailscale 访问：`http://<Tailscale-IP>:8328`

## Agent 工作台

当前实现只管理本地 WSL，不显示 SSH 设备矩阵。客户端按钮互斥，一次只操作一个真实检测到的客户端。

支持的适配器：Claude Code、Claude Desktop、Codex、Gemini CLI、Grok Build、OpenCode、OpenClaw、Hermes。页面只显示配置文件存在或二进制可检测到的客户端；缺少稳定写入适配器的能力只读展示。

- **Provider**：支持任意 HTTP(S) 上游。官方 API、自建中转、OpenRouter、CLIProxyAPI 都只是 Provider，不绑定某个项目。API Key 和敏感 Header 进入 `0600` 私有存储，HTML/API 只返回脱敏值。
- **Route**：独立 `wsl-agent-router.service` 监听 `127.0.0.1:7888`，按客户端命名空间执行协议转换、模型映射和转发。可逐客户端接管配置并恢复原文件。
- **出站代理**：Router 到 Provider 的 HTTP/HTTPS/SOCKS5 网络代理，和 Provider 中转是两层配置。
- **MCP**：先扫描当前客户端真实配置，再同步安装/更新或卸载。删除本地库定义不会修改客户端。Codex TOML、Claude/Gemini/OpenCode/OpenClaw JSON、Hermes YAML 分别写入并回读验证。
- **Skills**：支持本地目录、Git URL、ZIP；可复制或软链接到当前客户端独立 Skill 目录。要求 `SKILL.md`，ZIP 做路径校验，卸载项移入备份目录。
- **Prompts**：可导入当前提示词文件、保存模板、原子应用并恢复接管前内容；外部修改时保留现场并报告冲突。

直连模式是“客户端 → Provider”；本地路由模式是“客户端 → Agent Router → Provider”。点击 Provider 的“当前”会把该上游写入 Router 私有配置，开启客户端接管后才切换请求链路。

## 本地开发

```bash
uv venv
uv sync --extra dev
. .venv/bin/activate
uv run uvicorn app.main:app --host 0.0.0.0 --port 8328 --reload
```

## 测试

```bash
pytest -q
ruff check
```

## 安装为 systemd 服务

```bash
bash scripts/check_sudo_rules.sh
bash scripts/install_systemd_service.sh
bash scripts/install_agent_router_service.sh
curl -I http://127.0.0.1:8328/healthz
curl -fsS http://127.0.0.1:7888/health
```

systemd 服务名：

- `wsl-ops-panel.service`
- `wsl-agent-router.service`

## 运维文档

详见：

- `docs/operations.md`
