# Agent 后续修复：提交与本地加载验收（2026-10-06）

代码提交：`da8cc31ac3f6d2579aba3ee32d10378ebf908875`。用户在本轮修复门卡后回复“可以”，批准独立审查、提交及本地加载；未推送。

## 审查与测试

- 基线 `493a3cd2f09163025bfd8895760afaf1af011cc9`；Standards / Spec 两名独立只读审查者各一轮。
- Standards 无硬性问题；Spec 唯一观察经复核为 Anthropic 结束标记兼容问题，已补修，2 条回归先红后绿。两轴无未解决项。
- 提交前从暂存区导出独立源码快照：**731 passed（71.94s）**；与最终提交的 11 个文件逐字节核对一致。
- **70 项专项、45 项前端、8 类隔离 Chromium 交互**通过；Ruff、JS 语法和 diff 检查通过。
- [双轴审查及补修](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-remaining-20261006.md)、[原始缺陷与红绿证据](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/remaining-fixes-verification-20261005.md)。

## 本地加载

- 两个服务现场均为 `CanReload=no`；确认队列和 Router 请求空闲后，在线备份 SQLite、面板配置、接管状态、客户端原生文件和旧源码，再执行受控短重启。不是零停机 reload。
- Panel PID：`3220883 → 1029468`。
- Router PID：`3220885 → 1029466`。
- 重启及健康确认流程耗时约 **0.88 秒**，不是精确停机时长。
- 本机 `8328/healthz`、Tailscale `100.126.43.55:8328/healthz`、Router `7888/health` 均 **200 / ok**。

## 正式页面与不变性核验

- 登录态访问首页、Agent、任务页、Docker 分类、Agent library：均 **200**。
- 正式 `app.js` 与提交源码逐字节一致，SHA-256：`9b16fef46b60fae22fa51df14acddff81ab4c60db4937279323249f58bafd3f3`。
- Chromium 正式页面切换实际检测到的 **5 个客户端**：Claude、Codex、Gemini、OpenCode、OpenClaw；Providers / Route 页签正常，`pageErrors=[]`、`attemptedWrites=[]`。
- Grok Build / Hermes 本机未检测到；它们没有被虚构为实际已安装。Grok Build 的本轮原生格式与 writer 已通过隔离测试；未擅自安装任何客户端。
- **38 个业务容器前后一致**：ID、镜像、状态、启动时间、重启次数及挂载。
- 客户端原生配置、面板配置和 Provider/Router 数据库行前后校验一致；最终队列和 Router 活动请求仍为 0。
- 原生写入与增删逻辑测试均在临时 HOME / SQLite 上运行；正式页面验收为只读，没有向真实客户端写入测试 Provider/MCP，也没有调用真实上游 API。没有把隔离验收混称为正式上游全功能验收。
- 用户原有通知配置改动、`.ccg/`、`artifacts/` 和根目录审计产物保留，未纳入提交。

## 证据及回滚

- [加载结果](/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/rollout.json)
- [正式页面](/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/live-pages.json)
- [正式浏览器](/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/live-browser.json)
- [最终状态校验](/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/verification.json)
- 私有备份：`/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/pre-rollout/`；审查及测试原始证据：`/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/review-evidence/`。
- 浏览器临时登录凭据文件在验收后删除；未覆盖上一轮 ABCD 备份。
- 如需回滚：等队列和请求空闲，停止面板/Router，用备份的基线源码归档恢复代码后重启；本轮未修改业务数据或客户端配置，不应覆盖用户后续数据库变化。
