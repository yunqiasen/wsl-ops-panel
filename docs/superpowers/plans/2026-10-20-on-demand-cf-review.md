# 按需 CF · #6 最终审查与处理

固定点：`89058c65f3cf83458ef0b331353e848395bcf409`；审查输入 HEAD：`9a5e2281662ae1225014849143195c9f5afc9401`。

按 `implement` 发起一次独立的 Standards / Spec 并行只读审查，两者退出码均为 0；没有重复审查 #5，也没有为 #6 再发起第二轮审查。完整输入、原报告及日志保存在本轮证据目录 `review-ticket6/`。以下保留原报告，后面的处理记录区分成立项与误读。

## Standards · 原报告

## Standards 轴审查结果 — #6

审查基线 `89058c6` → HEAD `9a5e228`，范围仅 Standards（文档化标准违反 + baseline smells）。未发现文档化标准的硬违反（CONTEXT.md / ADR-0001 的范围约束——业务文件不动、历史域名只作线索、明确关联才控外部 unit——代码均遵守）。

### Baseline smells（均为判断项）

**Duplicated Code — 60 秒有效期窗口**
`app/services/endpoints.py:179` 和 `app/services/cf_tunnels/__init__.py:204` 各自硬编码了同一段新鲜度逻辑：`-0.001 <= age < 60`，且 `__init__` 内部 `_observe_record` 也用相同窗口（行 ~204 附近 `if not -0.001 <= age < 60:`）。三处魔数 60 / -0.001 重复出现，应提取为共享常量或 helper（如 `is_within_freshness_window(verified_at, now, window=60)`），供 status 读取和通知端点共用。当前改一处需同步改三处，容易漂移。

**Feature Envy — `_reconcile_requests` 越界操作 task store 对象**
`app/services/cf_tunnels/__init__.py:242-245`：
```python
task.status = "interrupted"
task.finished_at = datetime.fromtimestamp(self.clock(), UTC)
self.task_store.update(task)
```
该方法直接修改 `TaskStore` 返回的 `Task` 对象的属性再回写，对 Task 的内部结构（status/finished_at）和 store 的更新协议有强依赖。应将"标记中断"逻辑收进 TaskStore（如 `task_store.mark_interrupted(request_id)`），CF 服务只调用接口，不触碰 Task 内部字段。

**Primitive Obsession — `http_error_message` 用 `str(error).lower()` 子串匹配做网络分类**
`app/services/cf_tunnels/http_probe.py:13`：`detail = str(error).lower()`，随后用 `"dns"` / `"certificate"` / `"ssl"` 等关键字子串匹配分类错误类型。`str(error)` 可能包含请求 URL，若 URL hostname 含 `dns`（如 `dns-cf.example.com`）或路径含 `ssl`，会误分类。属 baseline smell：用字符串匹配替代 `isinstance` + 结构化分类。已有部分 isinstance 分支（TimeoutException/ConnectError/RemoteProtocolError），建议完全走 isinstance 路径或用 httpx 异常属性，减少对消息文本的依赖。

其余 hunk 未发现需报告的标准违反或 baseline smell。

## Spec · 原报告

审查完毕。以下为基于规格的发现报告。

---

## (a) 缺失/部分实现

**1. Agent2API 正式 Web 验收未完成** — 已知交付项，非代码缺陷。规格"正式 Web 验收与完成条件"第 1–5 步及 #6 验收标准最后一条均未执行，#6 据此保持开启。✅ 符合预期。

其余规格要求（T07/T09–T13/T15–T17 覆盖、生命周期容错、旧隧道兼容、有效期/重启/失败收尾、通知不复活过期网址、SSE 限制说明）均有对应实现和测试。

## (b) 超出规格（scope creep）

无实质越界发现。`NotificationService` 增加 `cf_service` 依赖属于规格"通知模块只消费同一份真实 CF 网址结果"的实现需要，非额外功能。

## (c) 已实现但可能有误

**1. `maintain()` 公网核验成功后未调用 `activate()`**
- 文件：`app/services/cf_tunnels/__init__.py`，`maintain` 方法中公网核验成功分支
- 对比：`execute()` 在同一成功路径调用 `self._runtime_for(handle).activate(handle, timeout=...)`，`maintain()` 仅 `store.update(state="online", ...)` 后直接继续
- 影响：`SystemdTunnelRuntime.activate()` 写入 `verified` 标记文件，但该文件在真实运行器的 `observe()`/`recover()`/`stop()` 中均未读取，故无生产影响。属一致性缺陷，非规格违反

**2. `maintain()` 公网核验成功后未二次核对源站归属**
- 文件：`app/services/cf_tunnels/__init__.py`，`maintain` 方法
- `execute()` 在 probe 成功后调用 `_revalidate_target()` 确认源站在核验期间未变化（规格："业务动作开始前再次核对项目归属和当前候选"）；`maintain()` 仅在 probe 前通过 `prepare(asset)` 检查 `target["revision"]`，probe 后直接 `store.update`
- 触发条件：周期维护窗口内源站实例在 HTTP 核验响应到达后、状态写入前被替换
- 影响：理论上极窄竞态可导致 `state="online"` 指向已失配源站；但 `maintain` 单次 probe ≤5s，窗口远小于 `execute` 的 90s 循环，且下一轮 30s 维护会捕获失配。规格未显式要求周期 probe 后二次核对，属低风险

**3. `read_cf_url` 与 `CFTunnelService` 使用不同时间源**
- 文件：`app/services/endpoints.py:170`，`read_cf_url` 用 `datetime.now(UTC).timestamp()`；`CFTunnelService._observe_record` 用 `self.clock()`（即 `time.time()`）
- 影响：测试冻结 `self.clock` 时两者不一致，但 `read_cf_url` 前有 `state != 'online'` 守卫，`status()` 已将过期记录置为 `error`，故实际不会返回过期网址。非规格违反，属代码一致性瑕疵

---

**结论：无阻断性 Spec 发现。** 交付待办（Agent2API 正式验收）与代码正确性发现已分开列示。上述 (c) 项均为低风险一致性缺陷，不影响 #6 代码审查通过。

## 处理记录

### Standards

- **有效期判断重复：已处理。** `cf_tunnels/freshness.py` 统一 60 秒有效期与微秒取整容差，状态和通知使用同一函数。原报告提到“三处”，实际是两个消费者。
- **TaskStore 边界建议：保留现有公开契约。** `TaskRecord` 是公开数据模型，`TaskStore.get/update` 是既有协议；任务执行器/工作线程也通过这个协议更新记录。CF 只按自身宿主重启语义标记对应任务，未访问存储私有字段。增加单用途的 `mark_interrupted(id)` 不会改善现有边界，本项为判断建议而非缺陷。
- **网络层误分类：已复现并修复。** URL 包含 `dns/ssl` 时错误归层，以及底层 DNS/TLS 原因被外层异常遮住，3 个回归先红后绿。现在先沿异常因果链识别系统/TLS 类型，仅对缺少结构化原因的旧传输保留去除 URL 后的明确错误短语兼容。

### Spec

- **周期维护不重复 activate：维持生命周期设计。** `runner.py` 确实读取 `verified` 文件（原报告关于它没有生产作用的描述不准确）。只有开启任务完成核验才写常驻标记；已核验连接的周期读取无需反复激活。中断且未核验的启动走精确清理/外部待核对分支，而非靠后台核验转为常驻。
- **“公网 probe 后缺少源站复核”：原报告误读。** 审查 HEAD 已包含 `b673a2e` 的末次 `_revalidate_target()`，对应回归已经复现并修复维护中的源站替换。保留其红/绿证据，没有重复添加同一修复。
- **时间源一致性：已统一判断实现。** 生产中的 `datetime.now(UTC).timestamp()` 与 `time.time()` 原本均是系统 epoch 时间；现在两个消费者共用有效期函数并明确传入 epoch 时钟。服务的可注入时钟继续只用于隔离测试。
- **正式验收：仍为交付待办。** Agent2API 正式网页和真实公网链路尚未执行，#6 保持开启。

### 同期补充核查

- **正式服务环境缺少用户会话变量：已确认并修复。** 读取正式面板 PID 的环境，仅做用户管理器版本查询，原命令返回 `Failed to connect to bus: No medium found`。现在为 CF 子命令补齐当前用户已存在且归属正确的运行目录/会话 socket；已有显式配置和父环境保持原样，未创建会话、开启 linger、变更 sudo 或重启服务。使用同一正式进程环境只读复测，管理器查询退出码为 0；新托管/旧 user-unit、自动/显式环境的 4 个隔离回归通过。
- **登录限制和历史网址展示：已先红后绿。** 已核验的 401/403 显示“可达，需要登录”；撤下的网址只作为可展开文字记录，没有打开链接。真实浏览器增加登录场景，并覆盖初始渲染与轮询、关闭后的历史展示。
- **旧 unit 额外生命周期命令：已先红后绿。** unit 文件或运行中的 drop-in 带 `ExecStop` 等额外命令时，保留外部线索/明确错误，避免关闭 CF 时顺带执行业务停止命令。源文件与实时查询均核对。

最终索引代码快照验证：984 Python、46 Node、5 个 CF 浏览器场景、既有 Agent 浏览器回归以及 Ruff/JS/diff 全部通过；逐项证据归入执行记录。正式合入、推送、加载及公网验收仍按交付门禁执行。

汇总：Standards 3 项判断建议（2 项处理、1 项按公开契约保留）；Spec 0 项确认的代码违规（3 项低风险观察已核对），另有 1 项正式验收待办。
