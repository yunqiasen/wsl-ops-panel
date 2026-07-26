# WSL Ops Panel

WSL 维护更新管理面板。

一个面向 WSL 环境的运维控制面板，当前 Phase 1 已完成：

- Docker 资产扫描、详情、整套 Compose 启停与后台串行执行
- Project 扫描覆盖维护目录和 user-systemd，多服务项目按整套服务启停
- systemd 资产扫描与 delete 动作执行
- Node 分类支持 npm 全局包四控
- Python 分类支持 Miniconda base 白名单包四控
- Agent 工作台按单客户端管理 Provider、Route、MCP、Skills、Prompts，只显示当前 WSL 真实检测到的客户端
- agent CLI 本体仍由 Node 分类维护，Agent 分类不重复更新 CLI
- 宿主机进程 / 系统基础设施继续只读扫描
- 任务中心、日志中心、终端中心、设置页
- 默认系统终端 + 可交互调试终端
- registry 重载
- 入队任务由后台 worker 自动按顺序执行

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
