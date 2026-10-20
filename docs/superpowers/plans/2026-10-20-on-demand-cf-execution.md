# 按需 CF 执行记录

父规格：GitHub #3；用户在规格到实现的交接卡之后回复“进入”，批准全部实现范围。实现位于独立工作树 `cf-on-demand-20261020`，正式工作区与运行服务保持原样。

## 已固定的测试接缝

已登录 HTTP 请求 → 实际动作计划与串行工作线程 → CF 状态和网页。只替换 Docker/systemd/HTTP/时钟这些外部依赖；状态存储用临时 SQLite。全部 Python 运行通过 `scripts/test_isolated.sh`。

## 文件职责

- `app/services/cf_tunnels/`：入口准备、状态存储、托管运行及生命周期；对外统一提供准备、计划/执行、状态。
- `app/api/cf_tunnels.py`：两个只读 CF 接口，复用现有登录与项目读取。
- `app/api/assets.py`、`app/api/bulk_actions.py`：保留旧动作契约，将 CF 动作交给统一模块。
- `app/adapters/base.py`、`app/tasks/worker.py`、`app/tasks/executor.py`：CF 有界动作参与既有真实任务执行。
- `app/api/overview.py`、`app/services/endpoints.py`、模板及静态前端：真实 CF 状态展示、入口选择、当前网址，保留其他模块职责。
- `tests/test_cf_tunnels.py`、外部依赖适配回归与前端回归：按票逐个添加行为，逐个确认红绿。

## 顺序与门禁

- [ ] #4：单入口 Docker 的默认关闭、准备、开启/核验、关闭、独立运行；完成本票最终审查后记录结果。
- [ ] #5（依赖 #4）：容器网络、Project、多入口与重新解析，完成本票最终审查。
- [ ] #6（依赖 #5）：并发、重启、超时收尾、旧资源、网页完整回归，完成最终审查及正式交付验收。

每个行为：先写一个失败测试 → 隔离运行确认原因 → 最小实现 → 专项检查 → 本地工作树提交。每票由 `implement` 统一发起一次最终双轴审查，嵌套 TDD 不重复审查。正式分支合入、推送及服务加载沿用独立交付范围。

## 当前切片

#4 首个行为：读取新项目 CF 状态为关闭，且没有创建隧道配置、运行资源或任务。测试：`bash scripts/test_isolated.sh tests/test_cf_tunnels.py`；预期红色为缺少 CF 读取接口（404），随后增加只读状态模块并返回关闭状态。

## 已取得证据（仅实现过程）

- 隔离初始基线：832 passed。
- #4 状态读取：红色 404；新接口与页面路由回归 26 passed。
- #4 单入口准备：红色 404；真实 Docker 观察格式加 HTTP 替身验证后 2 passed。
