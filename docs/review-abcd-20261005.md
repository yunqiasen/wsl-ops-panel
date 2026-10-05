# ABCD 双轴审查与补修（2026-10-05）

用户批准：“代码审查，没问题后提交并本地热加载”。固定基线：`b47be068d10730644d09dec47dabd170e780bbef`。
输入为 committed / staged / unstaged / untracked 修复内容的并集。两名独立只读审查者并行运行，各执行一轮；之后主审复现、补修和回归，没有再次调用审查。

排除修复前即存在的通知配置改动、`.ccg/`、`artifacts/`、根目录审计产物；这些内容保持在工作区，不纳入提交。

## Standards

没有发现本仓库文档规定的硬性标准违例。独立报告提出 4 项 Fowler 风格建议；其把部分 smell 称作“硬规则”的措辞不采纳，按技能规定全部作为工程判断处理，顺序保持原报告顺序。

| 原建议 | 处理 |
| --- | --- |
| usage 输入/输出 token 归一化在多协议收尾重复 | 采纳：抽出 `SseTransformer._usage_tokens()`，各协议仅映射自己的输出字段。 |
| Anthropic / Chat 结束原因存在两个方向的映射 | 保留：`end_turn` 与 `stop_sequence` 都映射到 `stop`，并非双射；直接反查会改变含义。两个方向各自表达协议规则。 |
| Provider 文件布局在回读与备份各维护一份 | 采纳：统一 `_NATIVE_FILES`；独立资源段常量放在 Provider 适配器供导入/比较共用。快照格式分支保留，因为客户端格式确实不同。 |
| 工作台对 Provider 服务调用较多，可能 Feature Envy | 保留：该函数负责组装 UI 的 current/added/saved/unknown/drifted 视图，原生比较已下沉服务；不增加只转发参数的包装层。 |

小结：4 项建议，2 项落实、2 项保留并说明理由；无未解决的硬性标准问题。

## Spec

### 独立报告（保持报告顺序）

| 报告项 | 复核 |
| --- | --- |
| Hermes YAML 损坏会使工作台 500 | 未复现，调用链不成立：Hermes 属累加客户端，工作台走 `live_provider_ids`，其读取器把 YAML 错误转成 `ProviderAdapterError(ValueError)`，外层已有捕获。新增真实页面及视图回归确认 200 / unknown。 |
| Responses 多文本块可能复用全局文本 | 推测性意见：当前只生成一个 `text` 输出块，没有报告所述“多个 text done 块”的触发链。未据此改动。 |
| Responses 目标不传递 reasoning | 范围说明：本轮验收聚焦文本、工具调用和终态，既有完整多模态/思考互转未列入通过声明；没有扩大承诺。 |

### 主审追加复现与修复

| 问题 | 证据与修复 |
| --- | --- |
| P2：独立 MCP 被误判为 Provider 漂移 | Codex、Grok Build、Gemini 的导入会剥离 MCP，而新回读曾比较整个原生配置。3 个真实读取回归先红；现在两边比较均排除独立 MCP 段，继续校验 Provider 扩展字段。 |
| P1：Gemini 无可选 ID 的不同工具被拼成一次调用 | 两个 SSE 事件分别携带完整 functionCall、均无 ID，旧代码复用每个事件的 index=0，参数拼成非法 JSON。先红后修；无 ID 时分配流内独立工具身份。 |
| P1：Router 接管与 Provider 写入争用原生文件 | 原锁只覆盖 Provider 与 Profile，Router 可在写入中途备份或覆盖同文件。并发回归先红；抽出 `native_client_lock`，接管启用/关闭及 stop 的恢复分支与 Provider 共用锁，接管快照取自完成后的配置。 |

小结：独立报告的 1 条声称缺陷经复核排除，2 项推测/范围说明不作为阻塞；主审确认 3 类问题均已补修。

## 验证

- `tests/test_abcd_review_regressions.py`：6 项，包括 5 条先红后绿断言及 Hermes 反证。
- 补修专项：115 passed。
- 工作区隔离全量：661 passed；前端 45 passed；Chromium 8 类交互、pageErrors=[]；原架构 5 条复现通过。
- 提交前从暂存区导出独立源码快照：661 passed（60.12s），结果保存在下列证据目录。
- Ruff、JS/shell 语法、diff 空白检查通过；所有写入测试均使用隔离 HOME、数据库和原生配置。没有把通过测试等同于正式服务已加载。

原报告、四种 diff、红绿日志：`/tmp/wsl-ops-abcd-review-20261005/`。
本地加载前需确认任务队列和 Router 请求空闲，备份配置/SQLite/代码，再重启面板与 Router；现有 systemd unit 的 `CanReload=no`，这是受控短重启，不是零停机 reload。业务容器和用户客户端配置只做前后校验。
