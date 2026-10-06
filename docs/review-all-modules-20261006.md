# 全模块修复：代码审查与处理（2026-10-06）

固定基线：`70764bf8e7baed96682e28af760af02abdc6326b`。
本轮用户明确批准“进入代码审查，处理审查发现后再提交”。一次正式审查，两名独立只读审查者并行完成（均退出 0）；主执行者复核并补修，没有再次调用审查。

输入覆盖 committed / staged / unstaged / 本轮新增文件。初始 committed 与 staged 为空。本轮原有 34 个修复文件，加本文；排除用户原有通知配置、`.ccg/`、`artifacts/` 和根目录旧审计文件。

标准：用户工程规则、README、operations、完整 Fowler smell 清单。当前目录及父目录未找到额外 AGENTS/CONTRIBUTING/CODING_STANDARDS。需求：用户 11 模块范围、原全模块架构审查 F01–F08/C6 及修复记录；基线后没有 issue 引用。报告原文和分层 diff 留在 `/tmp/wsl-review-20261006`。以下只列复核后有效发现，行号指修复后的文件。

## Standards

**3 组有效发现，已处理，剩余 0。**

1. **P3，过时映射**：`app/scanners/host_process_scanner.py:10`，固定端口到容器/unit 的死表及未消费归属字段使“仅作说明”名不副实。删除死表，保留 hint/purpose；同时清掉保证非空值的重复兜底。
2. **P3，重复策略分支**：`app/services/asset_policies.py:29`，protected 提前返回后又检查 protected，并重复构造禁用快照。合并同一阻止分支，既有 Node/Agent 包权限语义保持。
3. **P2，目录身份读取**：`app/services/project_lifecycle.py:17,99`，同一身份的 dev/ino 分别调用 stat，可能来自两次不同观测。复用一次 lstat 快照，删除前同样一次读取；这不是宣称消除所有外部文件系统竞态。

未采纳项：包版本列表相同三元分支在固定基线中已存在、未在本轮修改，且无行为错误，不按审查者标注的 P1 扩大修改。函数内导入用于避开依赖环，requires_sudo 是既有展示元数据，均不构成新缺陷。

## Spec

**6 组有效发现，已处理，剩余 0。**

1. **P1，F01 容器控制契约**：`app/services/host_ownership.py:108`，缺少 compose_service 使真实 Docker helper 抛 KeyError；补齐。新增 Compose/独立容器两条测试，不再只 mock helper。
2. **P1，F01 用户归属**：`host_ownership.py:36`，其他 UID 的 user service 被当成本用户 systemctl 目标。未匹配当前 UID 时仅观察，避免控制同名服务。
3. **P1，F03/F04 项目归属**：`project_lifecycle.py:58`、`project_scanner.py:229`，路径后缀碰撞及仅 unit 同名可误认其他项目。要求完整路径边界；名称仅对已确认归属的项排序。保留既有“父项目直接 unit 不吸收嵌套项目”的边界。
4. **P2，F07 安装前检查**：`app/api/remote_nodes.py:789-826`，自定义 python/pip 可能与 python3 不在同一环境。python -m pip 使用原解释器；pip/pip3 通过该启动器读取包列表，查询错误直接失败。标准本机/SSH路径不变。
5. **P2，F05/F06 并发追加**：`app/services/config_sync.py:302,341`，Windows 原有 compare/replace 之间可被另一追加任务穿插。增加 CreateNew + FileShare.None 锁，覆盖读取到验证；仅清理由自己获取的锁。此项为脚本结构回归，未做 Windows 真机并发验收。
6. **P2，文档一致性**：`docs/operations.md:89`，旧节仍称 Host/System 全只读。改成观察及已支持的受限维护，与 README/实现一致。

## 验证与交付

- 有行为变化的 Linux 场景均先得到失败回归，再修复并通过；Windows 新增生成脚本结构的先红后绿测试。
- 仅使用临时目录、模拟 Docker/systemd runner、临时 TCP 子进程；不执行真实包安装/卸载或业务资源变更。
- 新增回归共 8 条；既有父/子项目边界测试保留。探索中的“把所有嵌套 unit 加给父项目”被既有契约测试否定，已撤销该扩展，没有削弱原测试。
- 工作区隔离全量 **832 passed，74.29s**；前端 **45 passed**；原始场景当前执行路径 **8 passed**（F04 经 helper 验证，其余七条保留原断言）；Ruff、JS 语法与 diff 检查通过。
- 暂存区独立源码快照再次全量 **832 passed，73.20s**；同一快照前端 **45 passed**、原始场景 **8 passed**、Ruff/JS 检查通过。验证后仅补记本文结果，没有改变已测代码。
- 本轮只提交本地代码；未 push、未重启、未加载。Windows 分支仍仅生成器验收。
