# Agent / Compose ABCD 修复与回归（2026-10-05）

状态：用户已批准代码审查、提交和本地加载；双轴审查及有效发现补修完成。详见 `docs/review-abcd-20261005.md`。

固定基线：`b47be068d10730644d09dec47dabd170e780bbef`，分支 `phase1-complete`。
范围：本地 WSL；保持现有数据库事实源、客户端原生文件投影和 Docker 执行身份检查。
本轮未提交、未推送、未重启服务、未部署，未操作真实客户端配置或业务容器。

## A. Provider 当前状态早于实际写入

- 最小复现：队列接受切换请求，writer 退出 7 / 返回 0 但未写入，数据库却已指向新 Provider。
- 根因：API 入队后直接设置 current，任务执行与数据库状态没有统一成功边界。
- 修复：新增 `agent_provider_projection`，串起本客户端锁、资源/接管检查、写前文件快照、实际执行、原生回读、权限处理与 current 提交。异常恢复写前文件，保留原 current。
- 队列入口和 Profile 同步入口共用该生命周期；累加客户端移除在回读确认目标消失后才清 current，保留同文件其他 Provider。
- 原生快照按结构回读，覆盖表单外字段；原生快照优先于旧 routing 元数据。关闭写入密钥时，回读遵循原 writer 的凭证保留规则，包括嵌套 headers/options。
- 入队后资源被编辑或移除时停止旧计划；Router 在执行前已接管时停止直写。工作台把数据库 current 与原生文件差异显示为“配置漂移”。
- 任务页只展示 Provider 执行步骤名称，隐藏配置快照和 shell 编码载荷；磁盘计划沿用私有目录/文件权限。
- 回归：31 项，涵盖七客户端真实 writer、写入密钥开关、失败恢复、无效成功、外部漂移、累加移除、任务页脱敏、文件权限失败与过期资源。

说明：文件恢复针对被捕获的执行异常；本轮没有新增进程崩溃后的跨文件/数据库恢复日志。客户端自行改写文件仍通过下一次回读发现。

## B. 跨协议流式语义丢失

- 最小复现：Chat 的分段工具调用转 Anthropic / Responses 后消失；缺少终态的流被补成成功。
- 根因：旧转换以文本片段为中心，没有统一维护工具身份、参数累积及终态。
- 修复：每条流维护工具 ID/索引、参数片段、输出块、usage 和完成原因；覆盖 Chat、Anthropic、Responses、Gemini 之间的工具调用转换。Gemini 的对象参数在组装完成后输出。
- 错误、failed、incomplete 和无终态 EOF 输出对应失败；截断工具参数也不会发成功终态。Anthropic 需收到 message_stop；Chat 的 finish_reason 可作为明确终止信号，并保留后续 usage。
- Router HTTP 入口对转换后的协议内失败计失败，保留上游关闭、取消和计数幂等行为。
- 同协议流保持原字节透传；该路径未新增协议终态验证。多模态完整等价不属于本轮验收声明。
- 回归：41 项，四协议矩阵、拆包、双工具交错、UTF-8 单字节拆包、usage、长度结束、上游错误、缺终态、非法工具参数，以及真实 Router HTTP 入口和上游关闭。

## C. 客户端异步回包串台

- 最小复现：Codex 请求挂起，切至 Claude 后释放回包，队列被覆盖或客户端被切回 Codex。
- 根因：异步回包直接操作共享页面状态，没有携带客户端及请求代次。
- 修复：客户端选择代次 + 请求类别版本；旧回包丢弃，A→B→A 也淘汰旧 A 请求。同客户端详情请求只接受最新；用户输入使旧读取失效。
- 编辑草稿按客户端保留；队列加载时清旧列表并锁保存，迟到保存结果保留当前页面。Router 运行配置保存后续策略写入也绑定发起时客户端，切换后停止后续策略写入。
- 浏览器在真实后端模板和当前 app.js 上验证，网络全部拦截：队列迟到、详情迟到、草稿往返、A-B-A、输入覆盖读取、详情乱序、队列保存迟到、Router 保存迟到，共 8 类断言；pageErrors 为空。
- Node 前端全量 45 项通过，含新增状态单元测试。

## D. Compose 目标拼装重复

- 最小复现：runtime compose_files 为空时丢失配置的单文件；本地构建与拉取部署各自拼装参数，路径与重复文件处理不一致。
- 修复：`ComposeTarget` 统一项目目录、项目名、多文件、环境文件、有序去重及命令前缀；注册/发现通过 `capture_compose_context` 捕获同一执行上下文。
- 保持优先级：显式 compose_files → 实际运行标签 → 单文件 compose_file → 默认文件。单文件回退不会覆盖实际运行的多文件标签；需覆盖时配置 compose_files。
- 启停原容器仍按身份操作，不要求项目目录存在；部署前校验文件，原执行时身份核验、共享资源保护和多文件覆盖顺序保持。
- 回归：5 项新增契约测试，既有 Docker 生命周期和 API 回归全部通过。

## 最终验证

| 检查 | 结果 |
| --- | --- |
| 隔离 Python 全量 | 655 passed，60.60s |
| ABCD 新增 Python 专项（包含在全量内） | 77 passed |
| 原架构审查 5 条复现 | 5 passed |
| Node 前端全量 | 45 passed |
| Chromium 真实模板交互 | 8 类断言通过，pageErrors=[] |
| Ruff / JS 语法 / shell 语法 / diff 空白 | 通过 |

复跑（从项目根目录）：

```bash
bash scripts/test_isolated.sh
node --test tests/frontend/*.test.cjs
NODE_PATH=$(npm root -g) BROWSER_EXECUTABLE=/path/to/chromium bash scripts/test_agent_browser.sh
.venv/bin/ruff check --no-cache app tests
node --check app/static/app.js
sh -n scripts/test_agent_browser.sh
git diff --check
```

Python 使用源码副本、临时 HOME/数据库，writer 在临时客户端目录执行；Router 上游为 HTTP 夹具，Compose 为命令捕获夹具。浏览器只访问拦截响应，模拟保存未发往正式面板。这些结果不等于生产部署验收。

完整红/绿证据：`/tmp/wsl-ops-abcd-repair-20261005/`；关键文件为 `A-red.txt`、`A-raw-red.txt`、`A-stale-red.txt`、`B-red.txt`、`B-boundary-red.txt`、`browser-red.txt`、`C-router-red.txt`、`D-red.txt`、`targeted-final.txt`、`full-final.txt`、`frontend-final.txt`、`C-final.txt`、`original-repros-final.txt`。

修复前既有 `config/notifications/projects.yaml`、`.ccg/`、`artifacts/` 和 `agent-audit-20260713-130739.md` 保留原状，不属于此次修复。

2026-10-05 审查补修：共享 Provider/Router 原生写锁、排除独立 MCP 引起的误漂移、修复 Gemini 无 ID 工具合并；全量增至 661 项。经暂存区独立回归后提交，随后按用户批准进行本地受控加载。
