# Agent 后续三类缺陷：双轴审查（2026-10-06）

固定基线：`493a3cd2f09163025bfd8895760afaf1af011cc9`。
用户在修复门卡后回复“可以”，本轮 `diagnosing-bugs → code-review` 已明确批准。
两名独立只读审查者并行完成各一轮（退出码均为 0）；没有借用上一轮审查，也未二次调用审查。

输入：基线至 HEAD 的 committed、staged、unstaged 及本轮 untracked 全文并集。
排除修复前已有的通知配置修改、`.ccg/`、`artifacts/` 和根目录审计文件；它们保持工作区原状。
需求取自用户本轮三类缺陷、[修复验收记录](/home/div/1_Project_dir/AI/wsl-ops-panel/docs/remaining-fixes-verification-20261005.md)；标准取自 README、operations 文档和 Fowler smell 基线。
下列两轴保留独立报告顺序。报告行号及“尚未提交”指审查输入时的状态。

## Standards

### 独立审查结论：✅ 通过

#### 逐文件/逐 hunk

**`app/agent_router/transforms.py` L951–998**

无标准违规。`_responses_to_chat_response` 聚合逻辑正确：`text_parts` 以 `\n` 合并，`tool_calls` 保留全部调用身份，`finish_reason` 优先级 = incomplete(content_filter > length) > tool_calls > stop。验证调用链：

```
Responses output=[text("hello"), call("a"), call("b"), text("world")]
→ content="hello\nworld", tool_calls=[call_a, call_b], finish="tool_calls" ✓
```

`content` 三元链 `"\n".join(text_parts) if text_parts else None if tool_calls else ""` 运算符优先级正确（Python 解析为 `a if b else (c if d else e)`）。无 smell。

**`app/services/agent_native_lock.py` L11–23**

`exclusive_file_lock` 提取为通用原语，`native_client_lock` 委托给它。**possible Middle Man** — `native_client_lock` 仅加 `.projection-{client_id}.lock` 命名后委托。但语义命名有价值（客户端级锁 vs 通用文件锁），**suppress**。

**`app/services/agent_route_takeover.py` L10–140**

`enable`/`disable`/`status` 各自 `exclusive_file_lock(.route-takeover.lock)` → 委托 `_enable`/`_disable`/`_status`。锁顺序 docstring 声明 "controller lifecycle → native client → recovery journal"，journal 不反向获取外层锁。无 re-entrant 自死锁：`_status` 读后释放 journal 锁，`_disable` 重新获取。无标准违规。

**`app/services/agent_router_control.py` L53–130**

公/私方法分离正确：`stop()`→`_stop()`→`_disable_takeover()` 路径中，**仅公共方法** `stop`/`disable_takeover`/`enable_takeover` 获取 `.router-control.lock`，私有 `_stop`/`_disable_takeover`/`_enable_takeover` **不重新获取控制锁**。经实测 `flock(LOCK_EX)` 同进程同文件不同 fd 会死锁，故此设计正确避免了自死锁。调用链：

```
stop(restore_clients=True)
  → exclusive_file_lock(.router-control.lock)        # 控制锁
    → _stop()
      → takeover.status() → exclusive_file_lock(.route-takeover.lock)  # journal 锁，读完释放
      → _disable_takeover(client)
        → native_client_lock(.projection-{client}.lock)  # 客户端锁
          → takeover.disable() → exclusive_file_lock(.route-takeover.lock)  # journal 锁
```

锁序：控制→客户→journal ✓，无反向获取 ✓。

**`app/services/agent_providers.py` L1115–1711**

- `apply_grok_snapshot` → `apply_toml_snapshot` 重命名：消除 hardcoded "Grok Build"，错误消息改用 `app_id` 变量 ✓
- `preserve_target_resources(incoming, existing)`：单一职责，从 existing 恢复 `resource_sections` 中的键 ✓
- `toml_key` regex 从 `.isalnum()`（接受 Unicode 中文键→产无效 TOML）改为 `re.fullmatch(r'[A-Za-z0-9_-]+')`（ASCII-only，符合 TOML 1.0 bare key 规范）✓
- `toml_scalar` 增加 datetime 支持 ✓
- `is_secret_key` Codex 豁免 `requires_openai_auth`/`bearer_token_env_var`：这两个字段含 `auth`/`token` marker 但非密钥，豁免后 `merge_preserving_existing_secrets` 用 incoming 值而非保留 existing ✓
- **inline `import re`/`import datetime`** 在生成脚本函数体内 — 这是字符串模板的固有限制（生成代码运行在子进程，无模块级 import 上下文），**suppress**

**`docs/operations.md` L457–463**

与 `docs/remaining-fixes-verification-20261005.md` 一致：729+68+45+8，未提交/未加载状态标注正确。

#### 未发现的标准违规

- README.md / docs/operations.md 无被违反的规则
- Fowler 基线 smell 均为可接受的判断抑制项（Middle Man 有语义价值，inline import 是模板约束）
- 无 Duplicated Code（TOML/JSON 路径序列化逻辑不同）
- 无 Speculative Generality（`exclusive_file_lock` 已被 3 处实际调用）

**审查通过，无阻塞项。**

## Spec

### 独立审查结论：✅ 通过（1 项非阻塞性观察）

#### (c) 实现正确——三缺陷均已落实

**P1 Provider MCP 保留**：`preserve_target_resources`（agent_providers.py:1151-1157）对 Codex/Gemini/Grok Build 三客户端统一 pop incoming 资源段、从 existing 复制目标 MCP，在 `write_secrets` 两路均执行（apply_toml_snapshot:1167, gemini 分支:1640）。`PROVIDER_RESOURCE_SECTIONS`（adapters.py:20）覆盖 `mcp_servers`/`mcp`/`mcpServers`。Codex `is_secret_key`（:1320）正确放行 `requires_openai_auth`/`bearer_token_env_var`。调用链：`build_provider_apply_shell` → 生成脚本内 `apply_toml_snapshot` → `preserve_target_resources` → `write_text`；projection 层 `provider_projection`（projection.py:255-278）回读失败时 `_restore` 还原字节。✓

**P1 Responses 聚合**：`_responses_to_chat_response`（transforms.py:953-1004）聚合全部 `message` 文本（`\n`.join）与 `function_call` 工具到一条 assistant 消息；finish_reason 优先级 incomplete/content_filter > tool_calls > stop。调用链：app.py:534 `transform_response(target_format, source_format, decoded)` → Responses→Chat→Anthropic/Gemini 中间格式（transforms.py:74-88）。✓

**P1 journal 并发锁**：`exclusive_file_lock`（agent_native_lock.py:11-17）以 `fcntl.flock` 串行化线程/进程。锁序控制锁→客户端锁→journal 锁（agent_router_control.py:56-58, 106-108, 127-128；takeover:44-48, 93-95, 137-139）。`stop(restore_clients=True)` 在控制锁内调用 `_disable_takeover`（避免重入），逐客户端清 journal+DB 标记（:58-62, :128-130）。跨进程验证由 `test_journal_lock_also_covers_independent_processes` 覆盖。✓

#### (c) 非阻塞观察（推测，标注）

`_chat_to_anthropic_response`（transforms.py:898）finish 映射缺 `content_filter`。当 Responses `status=incomplete`+`reason=content_filter` 经 `_responses_to_chat_response`→`_chat_to_anthropic_response` 路由时，Anthropic `stop_reason` 透传为 `"content_filter"`（非 Anthropic 标准值）。spec 称"该中间转换也服务于 Anthropic/Gemini 输出"（remaining-fixes-verification-20261005.md），但测试仅在 Chat target 验证 content_filter。**推测**：Anthropic 无标准 content_filter 等价物，透传可能可接受，但未确认。

#### (b) 无范围蔓延

`toml_key` 正则、`toml_scalar` datetime 支持、`is_secret_key` 放行均为 MCP 保留/凭证策略的支撑改动，在范围内。`config/notifications/projects.yaml` 出现在 diff 中但已排除审查范围，不动。

---

**结论**：spec 三项需求及写密钥/回滚/锁序约束均已正确实现，无缺失或范围蔓延。上述观察为非阻塞推测，不影响通过。

## 主审复核与补修

Spec 的唯一观察已核实，不继续作为推测保留：

- Anthropic SDK 官方 `Message.stop_reason` 枚举包含原生内容过滤结束标记，不包含 Chat 的 `content_filter`。依据：[官方 Message 类型定义](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/message.py)，2026-10-06 取证；当时源码保存在证据目录 `anthropic-message-schema.py`。
- 复现公开转换入口：Responses incomplete/content_filter → Anthropic，以及 Chat content_filter → Anthropic；修复前 **2 failed**，实际输出非法枚举。
- `_chat_to_anthropic_response` 增加原生标记映射，保留已有正文；新增两个回归先红后绿。
- 本轮三类修复加审查补修专项：**70 passed**。没有修改请求路由、安装客户端或扩大到 SSH。

## 两轴汇总

- **Standards：0 项硬性违规、0 项待处理问题。** 命名包装及模板内 import 的风格观察在原报告中已说明保留理由。
- **Spec：1 项观察，经主审核实为 P2 协议兼容问题并修复；0 项未解决。** 缺失需求和范围蔓延均为 0。

审查补修后工作区隔离全量 **731 passed（70.02s）**；前端 **45 passed**、Chromium **8 类交互、pageErrors=[]**；Ruff / JS 语法 / diff 检查通过。暂存区独立快照全量测试仍作为提交门禁；加载及正式页面验收单独记录，不以隔离测试替代正式服务证据。

证据：`/tmp/wsl-ops-remaining-review-20261006/`，加载后持久化至 `/home/div/.local/state/wsl-ops-panel/remaining-review-20261006/review-evidence/`。
