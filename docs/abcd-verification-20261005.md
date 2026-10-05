# ABCD 本地加载与验收（2026-10-05）

代码提交：`5a1cffeec635873ff59c0d52dd613aadee56eb47`。用户批准审查、提交和本地加载；未推送。

## 审查与回归

- 固定基线 `b47be068`，独立 Standards / Spec 审查各一轮。
- 处理重复逻辑建议，并补修主审复现的 3 类问题：MCP 引起 Provider 假漂移、Gemini 无 ID 工具合并、Router 接管与 Provider 写入争用。
- 暂存区导出独立源码快照：**661 passed**，前端 **45 passed**，隔离 Chromium **8 类交互**、原始架构 **5 条复现**通过。Ruff / JS / shell / diff 检查通过。
- [双轴审查及处理记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-abcd-20261005.md)。

## 加载

- 加载前确认任务队列与 Router active 请求均为 0，在线备份 SQLite、配置、接管状态和原生客户端文件，同时保存基线代码归档。
- 两个 systemd unit 均 `CanReload=no`，使用受控短重启，不宣称零停机 reload。
- 面板 PID：`2840400 → 3220883`；Router PID：`324 → 3220885`；两者均 active。
- 重启及健康确认流程用时约 0.93 秒，该数值不等同于精确停机时间。
- `http://127.0.0.1:8328/healthz`、`http://100.126.43.55:8328/healthz`、`http://127.0.0.1:7888/health` 均 200 / ok。

## 正式页面核验

- 登录态访问首页、Agent、任务页、Docker 分类与 Agent library：均 200。
- 正式 `/static/app.js` 与提交源码逐字节一致，SHA-256：`9b16fef46b60fae22fa51df14acddff81ab4c60db4937279323249f58bafd3f3`。
- Chromium 在正式页面切换全部 **5 个实际检测到的客户端**（Claude、Codex、Gemini、OpenCode、OpenClaw），切换 Providers/Route 页签：`pageErrors=[]`，没有写请求。
- Grok Build、Hermes 在本机未检测到，因此没有虚构客户端按钮；其 Provider 格式/writer 已在隔离回归覆盖。本轮未安装客户端。
- 第一次 Live 浏览器脚本假定这两项已安装而等待超时；核实真实检测逻辑后，脚本按页面实际客户端清单验证。修正的是测试前提，未隐藏产品错误。

## 运行状态与边界

- **37 个业务容器前后完全一致**：ID、镜像、运行状态、启动时间、重启次数及挂载。
- 本机客户端配置文件、面板配置及 Provider/Router 数据库记录前后校验一致。
- 任务队列和 Router active 请求仍为空。没有启停业务容器、没有向真实客户端写入 Provider/MCP/Skill。
- 原生配置增删、流式上游和 Compose 操作链的功能回归使用隔离夹具；正式页面核验为只读，不把它混称为真实上游全功能调用验收。
- 本机通知配置及既有 `.ccg/`、`artifacts/`、根目录审计文件保持工作区原状，未纳入提交。

## 本机证据与回滚

- [加载记录](/home/div/.local/state/wsl-ops-panel/abcd-review-20261005/rollout.json)
- [最终状态校验](/home/div/.local/state/wsl-ops-panel/abcd-review-20261005/verification.json)
- [正式页面检查](/home/div/.local/state/wsl-ops-panel/abcd-review-20261005/live-pages.json)
- [正式浏览器检查](/home/div/.local/state/wsl-ops-panel/abcd-review-20261005/live-browser.json)
- [审查和回归证据](/home/div/.local/state/wsl-ops-panel/abcd-review-20261005/review-evidence)
- 备份目录：`/home/div/.local/state/wsl-ops-panel/abcd-review-20261005/pre-rollout/`，私有权限保存。
- 如需回滚，先等队列和流式请求空闲，再停止面板/Router；使用基线归档恢复源码后重启。数据库和客户端文件本轮未修改，不要无故覆盖用户后续数据。
