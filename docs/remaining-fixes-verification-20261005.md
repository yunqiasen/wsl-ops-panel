# Agent 后续三类缺陷：修复与隔离验收（2026-10-05）

固定基线：`493a3cd2f09163025bfd8895760afaf1af011cc9`。

状态：2026-10-05 完成修复与隔离初验；2026-10-06 用户回复“可以”，已完成本轮双轴独立审查，并补修 1 项协议结束标记兼容问题。代码 `da8cc31` 已提交并完成本地受控加载；[正式验收记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/remaining-verification-20261006.md)。用户任务为“一并修复和验收，没问题后提交并本地热加载”。

## 已复现的问题与修复

| 缺陷 | 最小复现 | 修复与边界 |
| --- | --- | --- |
| P1：切换 Provider 丢失独立 MCP | Codex、Gemini、Grok Build 原生文件带 MCP，导入 Provider 后切换模型；真实 worker 报成功，但 MCP 消失 | TOML/JSON 快照合并以目标文件当前 MCP 段为准；忽略 Provider 快照里的旧 MCP，目标已删除的 MCP 也不会被重新安装。异步激活与同步 Profile 共用真实 writer；回读失败恢复原文件、不提交 current。 |
| P1：非流式 Responses 转换只取第一条输出 | 一条文本加两条 function_call，转 Chat 后工具数为 0 | 聚合全部 output 文本与工具到一个 assistant 消息，保留 call_id、函数名和原参数。正常工具结束标记为 tool_calls；截断/内容过滤优先保留对应原因。由此也保护经 Chat 中间格式转换的 Anthropic/Gemini 响应。 |
| P1：不同客户端并发接管覆盖恢复记录 | Codex、Claude 同时读旧 journal，再分别写入；两个调用成功但仅剩一份恢复记录 | 独立 sidecar 文件锁保护 journal 的完整读改写及一致性状态读取，覆盖线程/进程。Controller 的控制锁串行化接管、恢复、DB 接管标记和 stop，避免不同实例覆盖标记及停止期间新接管。 |

### 实现细节

- 复用 `PROVIDER_RESOURCE_SECTIONS`，不另建客户端 MCP 字段清单。Codex：`mcp_servers`；Grok Build：`mcp_servers` / `mcp`；Gemini：`mcpServers`。
- 目标 MCP 凭证保持原值，不受 Provider 的“写入密钥”开关影响。Provider 关闭密钥写入时保留目标凭证；Codex 的 `requires_openai_auth` 开关和 `bearer_token_env_var` 变量名不是密钥，仍按新配置更新。
- TOML 需要合并时进行结构化重写，保留日期、时间、数组表和中文/带点键的值与类型；无结构变化时保留传入快照文本。重写不承诺保留原注释排版，writer 仍保留原文件备份。
- 锁顺序固定为 **Router 控制锁 → 客户端原生锁 → journal 锁**；journal 操作不反向获取外层锁。Provider 仍使用既有原生锁；stop 在控制锁内调用内部恢复方法，避免重入同一文件锁。
- 没有扩大到 SSH、客户端安装、上游真实 API 测速或 UI 改版。

## 红绿证据

1. 原始复现本轮重新执行：**5 failed / 1.40s**，症状与此前两轮一致。
2. 新测试纠正 Gemini 夹具字段名、去掉 Profile 重复参数后：**33 failed、3 passed / 4.24s**。失败来自 MCP 丢失/旧条目复活、工具丢失与并发事务冲突。
3. 修复后增加格式、凭证、失败恢复、独立进程和停止竞态覆盖。验证中发现 Codex 认证开关被误当密钥保留，新增回归先红，再修正字段分类。
4. 最终新增专项 **68 passed / 9.84s**。

原始并发复现使用 Barrier 强制两个事务在临界区内同时读取。正确加锁后，这种夹具会等待自身锁阻塞的第二个事务，因此保留原脚本作为红证据，新回归改用 Event：暂停第一个事务，确认第二个尚未进入，释放后验证两份 journal、DB 标记与逐个恢复结果。另保留原操作链及最终断言、移除强制重叠 Barrier，并重复并发场景 20 次：**24 passed / 2.56s**（4 个非并发用例加 20 次并发）。没有把夹具死锁当成修复失败，也没有声称旧 Barrier 脚本原样通过。

## 2026-10-05 隔离初验（审查前）

| 项目 | 结果 |
| --- | --- |
| 隔离 Python 全量 | **729 passed / 71.17s** |
| 本轮新增专项 | **68 passed** |
| 原操作链重跑 | **24 passed**，含 20 次双客户端并发 |
| 前端 Node | **45 passed** |
| Chromium 既有隔离交互 | **8 类断言通过，pageErrors=[]** |
| 静态检查 | Ruff、JS 语法及 `git diff --check` 通过 |

专项文件：

- [Provider / MCP 隔离、凭证及回滚](/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_provider_resource_preservation.py)
- [非流式工具及真实 Router 请求链](/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_router_nonstream_tools.py)
- [共享 journal、独立进程、停止恢复竞态](/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_route_takeover_concurrency.py)

所有写入验收使用临时 HOME、临时 SQLite 和客户端原生文件；Router 上游采用 HTTP transport 夹具。浏览器验收是既有界面状态回归，未把它表述为正式客户端安装或本轮全部后台行为的 UI 端到端验收。没有向本机日常使用的客户端写入测试 Provider/MCP，也没有启停业务容器。

## 提交与加载流程（已完成）

- 已完成：门禁批准、基于上述固定基线的 Standards / Spec 双轴独立审查及有效意见补修；[审查记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-remaining-20261006.md)。
- 本轮改动精确暂存；排除既有 `config/notifications/projects.yaml`、`.ccg/`、`artifacts/`、根目录审计文件。暂存区独立快照全量验收后提交，不推送。
- 加载前重新核对 Panel 队列、Router 活动请求和 systemd 配置；备份 SQLite、配置、原生文件及旧源码，再加载。
- 上轮两个 unit 的 `CanReload=no`，预计仍采用空闲后的受控短重启，加载时以现场值为准；不宣称零停机。
- 正式 HTTP/页面、代码版本、原生配置及 38 个业务容器不变性验收已通过，详见上述正式验收记录。

## 证据目录

- 工作日志：`/tmp/wsl-ops-remaining-20261005/`。
- 本轮持久化证据：`/home/div/.local/state/wsl-ops-panel/remaining-fixes-20261005/`。
- 未覆盖上一轮 ABCD 的审查、备份或加载记录。
