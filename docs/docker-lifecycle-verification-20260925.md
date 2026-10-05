# Docker 生命周期修复验收 — 2026-09-25

## 结论

修复已部署到 `wsl-ops-panel.service`，仍以 `div` 运行，未新增 sudo 权限规则。

- 35 项新增专项回归通过，前端 43 项通过，Ruff / JavaScript 语法 / 本轮文件 diff 检查通过。
- 隔离副本全量测试：**529 通过、2 项旧配置快照测试失败**。失败分别引用已不存在的 `openai_cpa_2/openai_cpa_3` 和 `docker__check-cx` 等旧对象；本轮没有恢复这些已删除业务，也没有跳过或删除测试。
- 正式面板接口下发 **14 条实测任务，全部 succeeded**：启停、重启、更新、指定版本部署、指定版本后再次更新、完全删除、失效 cwd 孤立容器启停/清理及 Metapi 原实例启动。
- 核验不仅看任务状态：容器 ID、运行/健康状态、实际 HTTP、部署标签变更、覆盖文件持久化、root:root 0700 目录、项目卷/网络、匿名卷均逐项检查。重复清理已删测试资源也通过。
- 前后 **33 个业务容器的 ID、镜像、运行状态、启动时间和挂载完全一致**。只重载面板，未重启业务容器；Metapi 启动为原实例幂等操作。
- 本机与 Tailscale 的 `:8328/healthz`、`:4010/api/desktop/health` 均 HTTP 200。任务页显示全部实测任务，队列空闲。临时容器、目录、卷、网络及注册配置已移除，保留任务审计记录。

## 范围与限制

- 这次修的是生命周期及部署上下文错误，不是扩大面板权限。真正缺权限时在删除资源前说明具体路径。
- 更新/指定版本实测使用本地已有 BusyBox 镜像和临时 buildable Compose 项目，`pull --ignore-buildable` 不依赖远端仓库；临时网络重试另有单元回归。上游网络、镜像仓库故障或项目专属部署变量缺失仍会明确报错。
- 部署失败恢复版本覆盖配置，但不会自动回滚数据库迁移或假称旧容器已恢复；部分重建失败需按实际服务状态处理。
- 共享资源、镜像、缺少归属证据的目录默认保留，不做全局 prune。
- Codex 只读复核指出的目录重叠、权限遍历、注册入口、临时覆盖文件、依赖就绪问题已补测试修复。Agy 复核因认证超时未完成，前端由主会话源码及测试验证。

## 证据

- [任务与状态核验](/home/div/.local/state/wsl-ops-panel/lifecycle-repair-20260925/verification.json)
- [14 条实测任务映射](/home/div/.local/state/wsl-ops-panel/lifecycle-repair-20260925/live-tasks.json)
- [业务容器操作前快照](/home/div/.local/state/wsl-ops-panel/lifecycle-repair-20260925/before.json)
- [业务容器操作后快照](/home/div/.local/state/wsl-ops-panel/lifecycle-repair-20260925/after.json)
- [专项回归](/home/div/1_Project_dir/AI/wsl-ops-panel/tests/test_docker_lifecycle_repair.py)
- [操作说明](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/operations.md)

未提交、未推送；保留此前本地未提交改动。
