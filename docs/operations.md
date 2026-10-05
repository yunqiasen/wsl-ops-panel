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

#### 数据库资源中心

`data/state.db` 是 Agent 业务事实源，保存 Provider、MCP、Skill 元数据、Prompt、Router、Profile、客户端变体、目标分配和回读观察。Skill 正文不作为 SQLite BLOB 保存，统一位于 `data/agent/skills/<skill_id>/`；数据库保存其路径和内容 Hash。客户端原生文件只是安装投影与回读现场，不会在扫描时自动伪装成数据库资源。

状态分四层：

1. 数据库资源：逻辑 MCP、Skill、Prompt 等。
2. 客户端变体：同一资源针对不同客户端和平台的原生差异。
3. 目标分配：`__local__ + client_id` 的期望启用状态。
4. 回读观察：`installed / drifted / missing / error / observed`。

页面动作语义：

- **导入到库**：把当前客户端发现项保存到数据库；不重写其他客户端。
- **安装/更新**：从数据库解析当前客户端变体，写入该客户端原生路径并回读。
- **卸载**：移除当前客户端投影与分配，保留数据库资源。
- **按库同步**：只修复已分配资源的缺失或漂移；无差异时明确返回“已与数据库一致”。
- **从库删除**：先卸载所有本地分配，再删除数据库资源；任一卸载失败时返回 `409` 并保留资源。

资源扫描只更新 discovery/observation。显式导入后才进入 managed library，扫描不会覆盖数据库中已有的客户端变体。

#### Provider 与 Route

Provider 以 `data/state.db` 为事实源。卡片直接显示数据库摘要中的端点、模型、协议、认证状态和真实客户端现场状态；编辑详情由服务端从原生配置与 Provider meta 生成，前端不再拼装通用 `routing` 空壳。凭证只保存在私有存储和目标客户端配置中，页面、普通 API、日志和文档不返回原值。

导入与保存语义：

- Claude Code、Codex、Gemini CLI、Grok Build 是**独占模式**：导入当前原生配置为 `default`；数据库已有用户记录时不自动覆盖。
- OpenCode、OpenClaw、Hermes 是**累加模式**：按客户端原生 Provider ID 分项导入，同一客户端可同时存在多条。
- “保存到数据库”只更新数据库，不改客户端文件；编辑当前项时保留当前状态。
- “复制”生成独立 ID，复制原生设置、meta、描述和排序位置；原生只读项的副本会转为可编辑数据库记录。
- “检测”只探测保存的 Base URL。收到任意 HTTP 响应即表示网络可达，业务层 `401/404` 不当成断网。

投影动作按客户端模式区分：

- 独占模式使用“启用 / 当前”。启用后写该客户端原生配置并把同客户端其他数据库记录取消当前状态。
- 累加模式使用“添加 / 移除”。添加只合并目标 Provider；移除只删客户端投影，数据库记录保留，兄弟 Provider 和未知字段保留。
- 客户端现场读取失败时显示“现场未知”，添加、移除和数据库删除均锁定，避免用数据库猜测客户端状态。
- 当前独占项、客户端仍存在的累加项和原生只读项不能直接从数据库删除。

Grok Build 原生配置位于 `~/.grok/config.toml`。导入时读取 `[models].default` 与对应 `[model.<profile>]`，保留完整 TOML、MCP 和未知字段；`responses` 映射为 `openai_responses`。`env_key` 只保存环境变量名，不把环境变量值写回 Provider 库或 TOML。只有官方登录、没有自定义模型表时显示官方配置快照。

Hermes 原生配置位于 `~/.hermes/config.yaml`。`custom_providers` 可导入、编辑、添加和移除；`providers` 字典显示为“原生只读”。增量写回保留兄弟 Provider、模型扩展和未知字段。

两种请求链路：

1. 直连：客户端直接使用当前 Provider。
2. 本地路由：客户端 → `127.0.0.1:7888/<client>/...` → 协议转换 / 模型映射 → 任意 Provider。

“出站网络代理”只控制 Router 到上游的 HTTP/HTTPS/SOCKS5 网络出口，不等于 Provider 中转。

Router 操作顺序：

1. 新建或导入 Provider；保存只进入数据库。
2. 启动 `wsl-agent-router.service`。
3. 对目标客户端开启“接管”。系统先备份原配置，再写本地 Router 地址并回读。
4. 接管态卡片主动作变为“切换路由 / 当前路由”；切换只更新 Router 上游，不重写客户端文件。
5. 关闭接管时恢复原配置；文件被外部修改时只恢复 Router 管理字段。
6. Router 停止前存在接管项时，页面先恢复客户端再停止。

Grok Build 接管只修改当前 `[model.<profile>]` 的 `base_url` 与代理占位 `api_key`。关闭时无外部改动则完整恢复；有外部改动时只恢复受管字段。官方登录空配置在写入前终止，原文件保持不变。

Router 运行配置保存在 `data/state.db`，数据库权限 `0600`。旧 `data/agent/router.json` 只在首次启动时迁移一次，迁移标记为 `router_json_migrated_v1`，之后 SQLite 始终优先。默认只监听 `127.0.0.1:7888`。

#### MCP

MCP 状态只来自当前客户端真实配置扫描：

- Codex：`~/.codex/config.toml`
- Claude Code：`~/.claude.json`
- Gemini CLI：`~/.gemini/settings.json`
- Grok Build：`~/.grok/config.toml`
- OpenCode：`~/.config/opencode/opencode.json`
- OpenClaw：`~/.openclaw/openclaw.json`
- Hermes：`~/.hermes/config.yaml`

MCP 主列表只读取数据库；现场独有项单独显示在“当前客户端发现”区域。同一逻辑 MCP 可以保存多个客户端变体，安装时优先使用当前客户端 `linux` 变体，缺省回退基础定义。

每次写入前备份，按客户端格式校验，临时文件原子替换，最后重新扫描确认。卸载只删除明确选中的 MCP，其他 MCP 和顶层配置保留。Grok Build 会把统一格式转换为原生 TOML，并按 `url`/`command` 判断 HTTP 或 stdio；Hermes 会保留 `enabled`、超时、工具、采样、Roots、认证和未知扩展字段。

#### Skills

来源支持本地目录、Git URL、ZIP。导入时先物化到临时目录，校验 `SKILL.md` 和安全路径，计算稳定目录 Hash，再原子替换 `data/agent/skills/<skill_id>/` 并写入数据库。导入本身不安装到客户端。

客户端安装只从统一 SSOT 读取；变体可选择 `copy` 或 `symlink`。安装元数据写入 `.wsl-ops-skill.json`，回读目录 Hash 后记录 `installed / drifted / missing`。ZIP 禁止绝对路径、`..` 和符号链接条目。卸载目标会先移到 `~/.wsl-ops-agent-backups/skills/`，数据库资源和 SSOT 保留；“从库删除”会在所有本地分配卸载成功后删除 SSOT。Grok Build 与 Hermes 分别使用 `~/.grok/skills`、`~/.hermes/skills`。

#### Prompts

逻辑 Prompt、客户端变体、目标分配和观察状态都保存在数据库。安装时优先解析当前客户端变体，没有变体才使用基础内容；每个目标客户端同一时间只分配一个 Prompt。

“导入当前 Prompt 文件”读取适配器声明的真实文件，例如 Codex `~/.codex/AGENTS.md`、Claude `~/.claude/CLAUDE.md`、Gemini `~/.gemini/GEMINI.md`、Grok Build `~/.grok/AGENTS.md`、Hermes `~/.hermes/AGENTS.md`，并同时保存逻辑资源、来源客户端变体、分配和 installed 观察，不回写文件。

安装或同步时先保存接管前文件和 Hash，再原子写入并回读验证。若应用后文件被其他程序修改，页面显示漂移；按库同步会恢复数据库内容。兼容备份状态位于 `data/agent/prompt-files/`，权限 `0600`。

#### Profiles

Profile 在数据库中保存按客户端划分的 Provider、MCP、Skill、Prompt 和 Router 资源 ID 组合。应用时先生成最小差异计划，只执行 `install / update / uninstall`；已一致项不会重复写入。Profile 新建、编辑、应用和删除都使用页面顶部唯一客户端选择器。删除 Profile 不删除资源库，也不修改客户端。

### 3.10 2026-07-28 本地 WSL 与 Grok/Hermes 验收

生产运行态仍按真实检测显示：当前 WSL 检测到 Claude Code、Codex、Gemini CLI、OpenCode、OpenClaw；`~/.grok`、`~/.hermes` 与对应二进制当前不存在，因此 Grok Build、Hermes 不出现在生产页面。面板不会为了展示功能而生成假客户端。

另用隔离临时 Home 做原生格式浏览器验收，七个配置客户端同时出现：Claude Code、Codex、Gemini CLI、Grok Build、OpenCode、OpenClaw、Hermes。已验证：

- Grok Build：真实 TOML MCP 扫描、Provider 导入、Route 接管与逐字节恢复、MCP 本地库保存、安装、卸载、删库。
- Hermes：`custom_providers` 与 `providers` 同时导入，后者显示“原生只读”；MCP、Skill、Prompt 面板使用 Hermes 原生路径。
- UI：只保留一个客户端选择区；桌面和 `390px` 窄屏无横向溢出。
- 浏览器：`consoleErrors=0`、`failedRequests=0`、`badResponses=0`。
- 清理：临时 Home、临时数据库、临时服务和测试对象全部删除，生产 `~/.grok`、`~/.hermes` 未被创建。

运行服务已重启：`wsl-ops-panel.service` 为 `0.0.0.0:8328`，`wsl-agent-router.service` 为 `127.0.0.1:7888`；两个健康接口均返回 HTTP 200。

2026-07-28 10:38 的最终门禁：`.venv/bin/pytest -q` 为 `413 passed`；前端 Node 测试 `14 passed`；Ruff、Python compileall、JavaScript 语法检查和 `git diff --check` 全部通过。另用临时 Home 运行 MCP / Provider / Skill / Prompt 全链路矩阵，增删改与客户端原生配置回读均通过。

### 3.11 2026-07-28 11:18 CC Switch 接管所有权与本地 WSL 复验

本轮继续按 CC Switch 的所有权规则复验：Router 接管客户端配置后，Live 文件归 Router 管理；“导入当前配置”和“直连应用”不能再覆盖它。面板同时检查持久化接管标记和真实 `route-takeover.json` 记录，避免状态文件短暂不同步时显示错误按钮。

- 接管态 Provider 导入和本地直连 API 返回 `409`，不保存代理占位配置、不创建直连任务；仅提交远程节点时不受本机接管状态影响。
- 接管态页面顶部客户端按钮带真实接管状态；当前客户端的“导入当前配置”“直连应用”立即禁用，关闭接管后立即恢复。
- Codex MCP 按原生格式回写 `type` 与 `http_headers`；写入完成后比较完整配置投影，不只比较 MCP ID。
- 关联回归：`tests/test_agent_workbench.py tests/test_agent_router.py tests/test_agent_route_takeover.py` 为 `117 passed`。
- 全量回归：`.venv/bin/pytest -q` 为 `420 passed in 53.48s`；前端 Node 测试为 `15 passed`；Ruff、compileall、`node --check`、`git diff --check` 全部通过。
- 生产浏览器实测 `http://127.0.0.1:8328/categories/agent`：真实显示 Claude Code、Codex、Gemini CLI、OpenCode、OpenClaw；Route/MCP/Skills/Prompts/Providers 五个 Tab 可切换；MCP 扫描返回 HTTP 200；`consoleErrors=0`、`failedRequests=0`。
- `390px` 页面无横向溢出；截图：`artifacts/agent-ccswitch-verified-desktop.png`、`artifacts/agent-ccswitch-verified-mobile.png`。
- `wsl-ops-panel.service` 监听 `0.0.0.0:8328`，`wsl-agent-router.service` 监听 `127.0.0.1:7888`；等待服务完成启动后 `/healthz` 与 `/health` 均返回 HTTP 200。

### 3.12 2026-07-29 数据库资源中心验收

本轮将本地 WSL Agent 工作台切换为数据库资源中心：

- `data/state.db` 成为 Provider、MCP、Skill 元数据、Prompt、Router、Profile、变体、分配和观察状态的业务事实源。
- 真实数据库副本迁移从 10 张表扩展到 23 张表；13 张新增资源表齐全，10 张原表行数逐表不变，迁移前后 `PRAGMA integrity_check=ok`，数据库权限为 `0600`。
- 隔离 Home 的七个客户端均按真实配置出现。Profile 完成新建、编辑、删除；混合应用真实执行“安装 3、卸载 2”，页面按动作准确汇总。
- Grok Build、Hermes 均完成 MCP、Skill、Prompt 的安装、无差异同步和卸载；回读状态与原生文件一致，原有 TOML/YAML 兄弟配置保留。
- 桌面 `1600px` 与移动 `390px` 的 Agent 六个 Tab 均无页面横向溢出；移动主导航改为紧凑横向滚动条。浏览器 `consoleErrors=0`、`pageErrors=0`、`failedRequests=0`。
- 完整门禁：Python `454 passed in 48.31s`；前端 Node `25 passed`；Ruff、compileall、`node --check`、`git diff --check` 全部返回 0。

证据截图：

- `artifacts/agent-database-center-desktop.png`
- `artifacts/agent-database-center-mobile.png`


### 3.13 2026-07-29 资源新增、描述与排序

Provider、MCP、Skill、Prompt、Profile 现在统一使用数据库资源交互：

- 五个分类标题均提供明确的“新增”按钮。新增时清空编辑器并开放 ID；编辑既有资源时回填完整内容并锁定 ID。
- 五类编辑器均可保存描述。Provider 描述写入 `notes`；MCP、Skill、Prompt、Profile 写入各自 `description`。列表名称下直接显示描述，空描述不占高度。
- 新资源追加到分类末尾，编辑保留原位置。Provider 顺序按客户端独立保存；其余四类保存全局顺序。
- 桌面端可从手柄拖拽排序；键盘和触屏使用“上移 / 下移”。排序立即写入 `data/state.db`，保存冲突或失败时页面重新读取数据库顺序。
- “保存到数据库”只更新资源库，不自动安装或改写客户端原生文件；安装、同步、卸载和从库删除仍是独立动作。

隔离 Home 的真实 UI 验收覆盖五类资源各两项：新增、描述编辑、ID 锁定、按钮排序、MCP 手柄拖拽、刷新后顺序保持、Provider 客户端隔离和逐类删除均通过。`1600px` 与 `390px` 页面无横向溢出；移动排序控件为 `44 × 44px`；`consoleErrors=0`、`failedRequests=0`、`badResponses=0`。

本轮完整门禁：Python `462 passed in 59.04s`；前端 Node `30 passed`；Ruff、compileall、`node --check`、`git diff --check` 全部返回 0。

证据截图：

- `/home/div/.codex/visualizations/2026/07/13/019f59e6-5f39-7140-9a7c-f8bfdfffb5d1/agent-resource-controls-desktop.png`
- `/home/div/.codex/visualizations/2026/07/13/019f59e6-5f39-7140-9a7c-f8bfdfffb5d1/agent-resource-controls-mobile.png`

生产回灌后，原工作区再次执行完整门禁：Python `462 passed in 60.31s`、前端 Node `30 passed`，其余检查退出 0。`wsl-ops-panel.service` 与 `wsl-agent-router.service` 已于 2026-07-29 11:08 重启，`8328/healthz` 与 `7888/health` 均返回 HTTP 200。生产浏览器真实显示 5 个客户端和 12 个 MCP；五类新增入口、MCP 新增清空、编辑 ID 锁定、桌面/移动宽度均通过，三类浏览器错误列表为空。


### 3.14 2026-08-01 Provider 原生工作台重构

本轮按 CC Switch 的 Provider 数据库与客户端投影逻辑重构：

- 七客户端使用各自原生字段表单；卡片直接显示端点、模型、协议、认证和数据库 / 当前 / 已添加 / 原生只读状态。
- 独占客户端完成启用与恢复；累加客户端完成添加、移除和兄弟配置保留；Hermes 原生只读项可查看、可复制，不可覆盖或删除。
- 新增、编辑、复制、检测、导入、数据库删除均从页面真实执行；保存未改客户端文件，检测本地端点返回 HTTP 200。
- Router 接管态热切换只更新 Router 上游，接管后的客户端文件 Hash 保持不变；关闭接管后原配置 Hash 恢复。
- 修复三项现场问题：编辑当前 Provider 不再丢失当前状态；接管切换后主动作立即显示“当前路由 / 切换路由”；客户端状态卡图标与配置来源随选择同步。
- 隔离 Home 同时检测七客户端；桌面 `1600px`、移动 `390px` 均无横向溢出，移动动作按钮高度不小于 `40px`；浏览器页面错误、控制台错误和失败响应均为 0。
- 完整门禁：Python `496 passed in 49.89s`；前端 Node `43 passed`；Ruff、compileall、JavaScript 语法和 `git diff --check` 全部返回 0。
- 限定 15 个实现文件回灌原工作区后再次执行完整门禁：Python `496 passed in 46.71s`、前端 Node `43 passed`，其余检查全部返回 0。
- 生产数据库在线备份后重启面板与 Router；`integrity_check=ok`、23 张表、权限 `0600`，迁移前后各表行数不变，Provider、Router、故障转移队列和 Profile 中无 `local-current` 引用。
- 生产页面真实检测 5 个客户端；Claude 与 Codex 的 `default` 卡片展示原生端点、模型和协议，详情凭证只显示脱敏占位；其余客户端没有数据库 Provider 时显示独立空状态。`8328/healthz`、`7888/health` 均为 HTTP 200，浏览器失败响应和页面错误为 0。

## 4. 常用命令

### 4.1 全量测试

```bash
/home/div/1_Project_dir/AI/wsl-ops-panel/scripts/test_isolated.sh
```

使用临时源码副本及 HOME/data，避免测试污染正在运行的面板。可在命令后传入测试文件路径或 pytest 参数。

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
- Agent 业务数据库：`data/state.db`
- Skill SSOT：`data/agent/skills/<skill_id>/`
- Router 旧配置迁移源：`data/agent/router.json`
- Prompt 兼容备份状态：`data/agent/prompt-files/`
- Skill 卸载备份：`~/.wsl-ops-agent-backups/skills/`
- 任务数据库：`data/tasks.sqlite3`
- 操作日志：`data/operations/`
- 终端日志：`data/terminals/`

## 6. Agent 数据库备份与恢复

### 6.1 在线备份

SQLite 处于 WAL 模式时使用 `.backup`，不要只复制单个 `state.db` 文件：

```bash
mkdir -p backups
sqlite3 data/state.db ".backup 'backups/state-$(date +%Y%m%d-%H%M%S).db'"
chmod 600 backups/state-*.db
sqlite3 backups/state-*.db 'PRAGMA integrity_check;'
```

同时备份 Skill 正文和旧 Prompt 兼容状态：

```bash
tar -C data -czf "backups/agent-files-$(date +%Y%m%d-%H%M%S).tgz" agent/skills agent/prompt-files
```

### 6.2 恢复

恢复会覆盖当前 Agent 资源和分配。先停止面板与 Router，再恢复数据库和 Skill SSOT：

```bash
sudo systemctl stop wsl-ops-panel.service wsl-agent-router.service
cp backups/state-TIMESTAMP.db data/state.db
chmod 600 data/state.db
tar -C data -xzf backups/agent-files-TIMESTAMP.tgz
sqlite3 data/state.db 'PRAGMA integrity_check;'
sudo systemctl start wsl-ops-panel.service wsl-agent-router.service
```

数据库恢复后，客户端原生文件不会被自动覆盖。进入 Agent 页面检查回读状态，再对需要的客户端执行“按库同步”。

## 2026-09-25：Docker 生命周期兼容修复

### 行为变化

- 现存容器的启动、停止、重启按实际项目标签及容器 ID 执行，不再用 `compose up` 代替启动。目录消失、部署变量未注入时仍可控制原容器；身份变化则提示刷新。依赖按标签排序，健康/一次性初始化依赖先等待，运行状态需连续稳定并排除重启循环。
- 注册与自动发现资产都接入 `app.services.docker_lifecycle`。同目录不同 Compose 项目分开识别；扫描通过结构化 inspect 获取标签，保留有序多个 Compose 文件、真实项目名。显式 `compose_files`、`compose_project`、`env_files` 可写入对象配置。
- 更新/部署先执行 `compose config --quiet` 再拉取和启动；只对镜像拉取的临时网络故障重试，避免重复删除或重建。未配置的部署变量仍需要项目自身的环境文件，绝不把容器应用凭证复制为插值环境。Metapi 现有实例启停不依赖 `METAPI_IMAGE`/`METAPI_DATA_DIR`，专用升级仍遵循 metapictl 的镜像校验流程。
- 指定版本使用私有、原子写入的 `.wsl-ops-panel.version.json`，成功后保留，避免 Compose 标签指向已删除的临时覆盖文件。失败时恢复原覆盖配置；如果已发生部分重建，任务保持失败，检查实际服务后再恢复。**不会自动回滚业务数据库或迁移。**
- 删除在面板工作目录执行，兼容项目 cwd 已消失。先完成路径、目录权限和共享资源检查再删容器；root 子目录仅在现有 sudo 规则允许该精确 `rm` 命令时清理，面板继续使用普通用户运行，未新增 sudo 规则。
- 完全删除只清理目标容器及无使用者的项目网络/卷；共享卷、共享/嵌套项目目录、缺少身份依据的目录及镜像保留。外部数据目录不作为项目目录递归清除。路径是软链接、广泛根目录、面板本身或含嵌套挂载时停止；删除标记在核验后写入。
- 批量操作显示后端跳过原因；入队与执行成功分开。正常 stderr 进度记为输出。中文系统日志使用增量 UTF-8 解码，并处理续传和文件轮转。

### 验证方式

- `tests/test_docker_lifecycle_repair.py` 覆盖失效目录、同目录多项目、结构化多文件标签、权限预检、共享资源、身份变化、首部署、指定版本后再次更新、依赖完成状态、重启循环、临时网络重试和中文日志。
- 在隔离副本执行全量测试，避免测试默认 `data/` 修改运行中的任务库。历史实机配置快照测试如仍断言已删除对象，应与当前配置漂移单独记录，勿恢复已删除生产实例来满足旧测试。
- Live 用 `/tmp/wsl-panel-repair-20260925/资源` 的临时 Compose 项目，经正式面板接口执行，再核验任务结束状态、容器身份/健康、HTTP、卷/网络/目录。业务只验证 Metapi 原实例启动；其他容器状态对比快照。

本轮最终结果：35 项专项回归、43 项前端测试通过；14 条正式接口任务全部成功，33 个业务容器状态未变。全量测试 529 通过，2 项旧实机配置快照断言失败；详见 [验收记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/docker-lifecycle-verification-20260925.md)。


## 2026-10-04 / 2026-10-05：五项可靠性补丁与审查补修

### 行为与兼容性

- **项目定位**：自动发现的 Docker ID 固定由规范化目录 + Compose 项目名生成，不再随同目录成员或扫描排序改变。注册对象的显式 ID 保持不变。旧的自动发现链接需刷新页面获取新 ID；旧展示编号及其删除标记不映射到新项目，避免误操作/误隐藏其他项目。新标记只命中对应稳定 ID，过期 Docker 卡片不从缓存恢复成可执行目标。
- **执行上下文**：拉取镜像与本地 Git 构建共用项目/容器身份检查；本地构建在准备和部署前检查，保留实际 Compose 多文件、环境文件、项目名与工作目录，并先运行 config 校验。自启开关按 inspect 得到的容器 ID 操作并回读策略。既有共享资源保护、镜像保留、失败后保留版本覆盖文件继续生效。
- **队列**：计划缺失、JSON/结构损坏、执行器异常只结束对应任务并写入 finished_at；诊断日志失败也不拖停后续任务。任务线程停止时 `/healthz` 返回 503/degraded，正常运行时仍为 200/ok。任务查询等暂时存储异常记录类型并等待下一轮，避免线程退出；持久存储故障仍需排查磁盘/数据库，线程存活不等于任务成功。坏计划补齐 started_at/finished_at。
- **Agent 观察**：MCP 扫描/归一化集中复用，解析错误和结构错误持久化为 error，不再留下可供对账使用的旧 installed。对账前重新读取本地配置；安装、更新、卸载、Profile 和同步均跳过读取失败对象并给出警告，修复配置并重扫后恢复。直接安装入口的读取失败也会更新错误观察，不回显原配置内容。
- **运行目录**：config_root 解析为绝对路径；命名为 config 的目录使用其同级 data，其他根使用根内 data，与 state.db 的解析规则一致。任务库、执行计划、系统日志、终端会话和上传目录按应用隔离；登录和页面标题使用该应用的 panel.yaml。自定义配置根应包含自己的 panel.yaml。原生 Agent 客户端文件仍遵循既有 HOME/客户端路径规则，不复制用户客户端配置。
- **启动恢复**：running → interrupted 仅在应用 lifespan 启动时执行；导入 app.main 或只构建应用不会将其他任务标记为中断。仍按单进程、全局串行任务队列使用。
- **批量选择**：每次请求同分类只扫描一次；重复勾选只入队一次，逐项保留跳过原因；扫描异常按分类隔离，详情异常按对象隔离，错误只返回类型而不回显配置，其他目标继续入队；HTMX 请求也返回约定的批量 JSON。执行阶段的身份检查继续生效，不以旧缓存代替现场校验。

### 审查前验证（历史）

- 专项 85 项、前端 43 项通过，Ruff 与 diff 空白检查通过。
- 全量 568 通过，2 项旧实机配置快照断言失败；对照修复前基线确认是同两项旧失败（基线 529 通过）。没有将其隐藏或恢复已删除实例。
- 端到端测试走真实 HTTP、队列和任务子进程，Docker 为文件驱动模拟夹具；不等同于生产 Live 验证。本轮未重启面板、未操作业务容器。
- 清单与证据：[五项修复 TODO](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/superpowers/plans/2026-10-04-runtime-reliability-repair.md)。待用户确认进入代码审查，之后再处理审查发现及上线验收。

### 2026-10-05 审查补修

- 已捕获的容器上下文遇到新名称/新 ID 时阻止操作并要求刷新，首次部署空上下文仍支持。
- MCP 扫描统一通过 Store，错误独立于资源 ID；名为 `__scan__` 的真实 MCP 不再污染错误区。
- 两个过时本机实例快照测试改成确定性配置契约测试；隔离测试子进程使用项目虚拟环境依赖。
- 双轴发现及处理见 [审查记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-20261005.md)。审查补修后暂存区独立快照全量 578 项通过，前端 43 项通过；本地已加载：8 条正式接口实机任务全部成功，37 个业务容器状态保持不变；详见 [验收记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/reliability-verification-20261005.md)。

## 2026-10-05：Agent ABCD 修复与审查补修

- Provider 切换入队不等于生效：执行、原生回读和权限处理成功后才更新数据库 current；失败恢复原文件。Profile 同步沿用同一流程。累加客户端移除先核验目标消失，再清 current，其他条目保留。数据库与本机不一致时显示“配置漂移”。
- 关闭“写入密钥”时，回读也遵守客户端凭证保留规则；七个 Provider 客户端均有真实 writer 夹具测试。入队后编辑资源会使旧计划失效。任务详情隐藏 Provider 执行快照及编码载荷。
- 跨协议 SSE 保留工具 ID、分段参数、usage 和结束原因；上游错误、截断工具参数或缺少终态时输出失败，并计入 Router 失败数。同协议仍原字节透传。
- 客户端切换后，旧队列/详情/保存回包不再覆盖当前页面；每个客户端保留编辑草稿。Router 保存操作绑定发起时客户端，避免保存途中切换造成策略串写。
- Compose 拉取部署与本地构建共用目标与参数拼装，保序去重。优先使用显式 compose_files，其次运行标签，再回退 compose_file；现有容器启停、执行时身份核验和共享资源保护保持。
- 审查补修后隔离全量 **661 项**、前端 **45 项**、原始 **5 条**复现及 Chromium **8 类**交互断言通过；本地加载记录另列。详见 [ABCD 修复记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/superpowers/plans/2026-10-05-agent-abcd-repair.md)。

- 审查补修：Provider 与 Router 接管共用原生写锁；MCP 独立配置段不算 Provider 漂移；Gemini 无 ID 的不同工具调用分配独立身份。[审查记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-abcd-20261005.md)。

### ABCD 本地加载结果

代码 `5a1cffe` 已加载：面板与 Router 受控重启，本机/Tailscale 健康检查通过；正式浏览器验证 5 个实际检测到的客户端，页面无 JavaScript 错误。37 个业务容器、原生客户端配置和 Provider/Router 数据前后不变。[验收记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/abcd-verification-20261005.md)。
