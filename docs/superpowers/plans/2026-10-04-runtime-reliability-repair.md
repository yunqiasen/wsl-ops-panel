# WSL Ops Panel：五项可靠性修复 TODO

基线 HEAD：`c68e8a17d982d130704e7195d9fda33fcdf0a27a`。开始时有 157 项既有工作区变更，保留。初始阶段不提交、不部署；2026-10-05 用户已批准审查、提交和本地加载。业务容器不作测试对象。

## TODO

- [x] 保存 HEAD 和修复前工作区源码副本，建立隔离回归。
- [x] 复现五类问题：8 个断言失败，包含 ID 漂移/误隐藏、坏计划停队列、虚假健康、观察错误、导入副作用、环境串用、批量重复扫描。
- [x] 01：固定自动发现项目标识，限定删除归属，补齐本地构建的执行目标检查。
- [x] 02：任务计划读取/执行异常统一结算，队列继续执行，健康检查反映 worker 状态。
- [x] 03：Agent 扫描错误落库并参与对账，避免使用旧观察盲目覆盖配置。
- [x] 04：任务库、计划、日志等按运行根隔离；恢复移至生命周期启动。
- [x] 05：批量选择复用同类快照，不削弱执行阶段身份检查。
- [x] 扩展边界回归、全量测试对照、清理临时调试、更新文档（两项旧配置快照失败单列）。
- [x] 用户批准进入 code-review，完成一次 Standards / Spec 双轴审查并补修有效发现。
- [x] 审查回归：修复身份绕过、批量错误隔离、MCP 名称冲突及 worker 查询异常。
- [x] 将两条过时本机快照测试转换为确定性契约夹具。
- [ ] 最终完整门禁、提交和本地加载/Live 验收。

## 反馈循环

`/tmp/run-wsl-ops-repair-tests.sh` 在临时 HOME/cwd 中运行 `tests/test_runtime_reliability.py`，不接触生产 data 或容器。初始结果：8 failed，1 worker 线程异常警告。临时证据与基线副本：`/tmp/wsl-ops-repair-20261004-1qy6rz0u`。

## 假设与预测

1. ID 取决于扫描成员和冲突排序：只改变成员即可改变旧 ID；稳定身份应消除漂移并限定删除对象。
2. 读计划位于异常处理之外：只破坏第一条计划就阻止后续任务；统一失败结算应让第二条继续。
3. 扫描失败跳过观察写入：只破坏客户端配置就留下旧 installed；错误观察应让对账提示并跳过写操作。
4. 默认路径/全局对象和构造期恢复：只改变 config_root 或导入模块就串用/修改状态；运行时装配和 lifespan 恢复应隔离。
5. 批量循环重复单项查询：数量为 N 时扫描 2N 次；请求内快照应降为每分类一次，执行时仍复核身份。

## 审查前验证快照（历史）

- 五项补丁完成，等待用户批准进入代码审查；未提交、未部署、未启停业务服务。
- 最小反例均已转绿：同目录两项目→移除其中一项；两条任务中首条计划损坏；单客户端配置由合法变为损坏；两个独立配置根；一批三个项目。
- 专项回归：85 passed（运行时可靠性、HTTP/worker/子进程端到端、Docker 生命周期、任务 worker）。
- 前端：43 passed；Ruff app/tests 全量通过；git diff --check 通过；无本轮临时 DEBUG 插桩。
- Python 全量：568 passed，2 failed。修复前源码副本同样为这两项失败，529 passed：
  - test_repo_openai_cpa_variants_use_shared_source_recipe_with_distinct_runtime_dirs：仍断言已删除的 openai_cpa_2 / openai_cpa_3。
  - test_repo_registry_pins_live_compose_files_primary_containers_and_endpoints：仍引用缺失的 docker__check-cx。
- 没有恢复已删除项目，也没有跳过或修改这两个旧断言来制造全绿。
- HTTP 端到端使用文件驱动的 Docker 模拟夹具，执行真实任务子进程：坏计划先失败，随后 start → update-latest → delete → start → full-delete 全部成功；未做生产 Docker Live 验收。
- 所有旧工作区变更保留；19 个生产文件的本轮变化以修复前文件副本为增量基准，避免混入既有未提交工作。

## 可重复命令

```bash
/home/div/1_Project_dir/AI/wsl-ops-panel/scripts/test_isolated.sh tests/test_runtime_reliability.py tests/test_runtime_reliability_e2e.py tests/test_docker_lifecycle_repair.py tests/test_task_worker.py
/home/div/1_Project_dir/AI/wsl-ops-panel/scripts/test_isolated.sh
```

脚本复制源码/配置到私有临时目录，隔离 HOME/data，退出后清理副本；第二条为全量；审查阶段已修正两条历史测试的夹具。

后续审查基线：HEAD `c68e8a17d982d130704e7195d9fda33fcdf0a27a`，外加 `/tmp/wsl-ops-repair-20261004-1qy6rz0u/baseline` 中本轮开始时的工作区副本。细分结果见同目录的 red.txt、baseline-tests.txt、full-final.txt、focused-final.txt、frontend.txt、e2e.txt。

## 审查进展

详见 [双轴审查记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/review-20261005.md)。暂存区独立快照全量 578 项 Python 测试通过，前端 43 项通过，Ruff、shell 语法及 staged diff 检查通过。
