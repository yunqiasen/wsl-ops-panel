# 2026-10-05 本地加载与验收

代码提交：`86ba544`。完成一次双轴审查，修复有效发现后提交；未推送。

## 通过项

- 暂存区导出到独立目录验证：Python **578 passed**，前端 **43 passed**；Ruff、JavaScript/shell 语法、staged diff 检查通过。
- 队列空闲后备份配置及 SQLite 数据库，仅重启系统级 `wsl-ops-panel.service`。PID **4423 → 2840400**，当前健康探针验证 worker 存活。
- 本机与 Tailscale 的 `:8328/healthz` 均 **200 / ok**。首页、登录页、Docker 分类、任务页均 **200**；静态 JS/CSS 与本地提交文件逐字节一致。
- 正式面板 API 下发 **8 条临时 Docker 任务，全部 succeeded**：停止、启动、重启、自启开、自启关、更新、普通删除、完全删除。
- 启动及自启批量请求故意重复同一 ID，均只入队一次。启停/重启保持原容器身份，自启策略按 inspect 回读。
- 更新测试将专用本地镜像标签从缓存 BusyBox 切换到缓存 Alpine，确认容器实际重建、镜像 ID 改变、卷数据保留。Compose 为 buildable，跳过联网拉取，不代表远端镜像仓库永远可用。
- 普通删除仅移除应用容器，目录/卷/网络保留；完全删除移除临时项目容器、专属卷、网络、目录，镜像由验收脚本最后仅清理自己的标签。
- 测试容器、标签、目录及专属删除标记均已清理，任务记录保留；队列为空。任务页按约定的短 UUID 显示全部 8 张任务卡。
- **37 个业务容器（18 个运行中）前后不变**：比较 ID、镜像、运行状态、启动时间、重启次数及挂载内容；挂载列表按目标/来源排序，忽略 Docker 返回顺序。

## 边界

- 服务没有 reload 能力，本次是短暂受控重启，不宣称零停机热更新；未启停业务容器，未新增 sudo 权限规则。
- 回归与 Live 覆盖上述修复及操作链；外部网络故障、持久数据库/磁盘故障和项目自身构建问题仍可能失败，会保留真实错误。
- 本机通知凭据、历史 `.ccg/` / `artifacts/` / 根目录审计产物保留在工作区，未提交。

## 本机证据

- [部署及备份记录](/home/div/.local/state/wsl-ops-panel/reliability-review-20261005/rollout.json)
- [最终核验](/home/div/.local/state/wsl-ops-panel/reliability-review-20261005/verification.json)
- [8 条任务记录](/home/div/.local/state/wsl-ops-panel/reliability-review-20261005/live-tasks.json)
- [回归及双轴审查证据](/home/div/.local/state/wsl-ops-panel/reliability-review-20261005/review-evidence)
- [审查发现与处理](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-20261005.md)
