import shlex
import subprocess
from time import monotonic, sleep
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.adapters.base import ActionPlan
from app.models.tasks import TaskRecord
from app.tasks.store import TaskStore
from app.terminals.system_terminal import SystemTerminalSink


def append_task_chunk(
    task: TaskRecord,
    chunk: str,
    *,
    sink: SystemTerminalSink,
    stream: Literal["stdout", "stderr"] = "stdout",
    mirror_to_system: bool = True,
) -> None:
    if mirror_to_system:
        sink.write(chunk)
    log_path = Path(
        task.stdout_log_path if stream == "stdout" else task.stderr_log_path
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(chunk)
    log_path.chmod(0o600)


def append_system_chunk(sink: SystemTerminalSink, chunk: str) -> None:
    sink.write(chunk)


_ACTION_LABELS = {
    "update_latest": "更新到最新版",
    "deploy_version": "部署指定版本",
    "delete": "删除运行实体",
    "full_delete": "完全删除",
    "start": "启动服务",
    "stop": "停止服务",
    "restart": "重启服务",
    "autostart_enable": "开启开机自启",
    "autostart_disable": "关闭开机自启",
    "cf_create": "生成 CF 访问链接",
    "cf_refresh": "刷新 CF 访问链接",
    "cf_disable": "关闭 CF 访问链接",
    "notify_send": "发送微信通知",
    "node_install_or_update": "安装/更新 Node 工具",
    "node_delete": "删除 Node 工具",
    "python_install_or_update": "安装/更新 Python 包",
    "python_delete": "删除 Python 包",
    "remote_node_install_or_update": "远程安装/更新 Node 工具",
    "remote_node_delete": "远程删除 Node 工具",
    "remote_python_install_or_update": "远程安装/更新 Python 包",
    "remote_python_delete": "远程删除 Python 包",
    "agent_provider_apply": "应用 Agent 供应商配置",
    "agent_mcp_sync": "同步 Agent MCP",
    "agent_mcp_apply": "安装 / 更新 Agent MCP",
    "agent_prompt_apply": "应用 Agent Prompt",
    "agent_skill_install": "安装 Agent Skill",
    "agent_skill_update": "更新 Agent Skill",
    "agent_skill_delete": "删除 Agent Skill",
}

_DEVICE_LABELS = {
    "local": "当前 WSL",
    "__local__": "当前 WSL",
    "mac-book": "MAC-Book",
    "win": "Win",
    "mimi": "MiMI",
    "vps": "VPS",
}

_PACKAGE_LABELS = {
    "openai-codex": "Codex CLI",
    "anthropic-ai-claude-code": "Claude Code",
    "google-gemini-cli": "Gemini CLI",
    "augmentcode-auggie": "Auggie",
    "jackwener-opencli": "OpenCLI",
    "qingchencloud-openclaw-zh": "OpenClaw",
}


def _friendly_action(action: str) -> str:
    return _ACTION_LABELS.get(action, action.replace("_", " "))


def _friendly_object(object_id: str) -> str:
    if object_id.startswith("remote__"):
        parts = object_id.split("__")
        if len(parts) >= 4:
            device = _device_label(parts[1])
            category = _category_label(parts[2])
            package = _package_label(parts[3])
            return f"{device} / {category} / {package}"
        return object_id.replace("__", " / ")
    if object_id.startswith("local__"):
        parts = object_id.split("__")
        if len(parts) >= 3:
            return (
                f"当前 WSL / {_category_label(parts[1])} / {_package_label(parts[2])}"
            )
        return object_id.replace("__", " / ")
    if object_id.startswith("project__"):
        return object_id.removeprefix("project__").replace("-", " ")
    known = {
        "cpa": "CPA",
        "sub2api": "Sub2API",
        "new_api": "New API",
        "searxng": "SearXNG",
        "wsl_ops_panel": "WSL Ops Panel",
    }
    if object_id in known:
        return known[object_id]
    text = object_id.replace("__", " / ").replace("_", " ").strip()
    if text.lower().startswith("openai cpa"):
        return text.replace("openai cpa", "OpenAI-cpa", 1).replace(
            "OpenAI-cpa ", "OpenAI-cpa-", 1
        )
    return text or object_id


def _device_label(value: str) -> str:
    return _DEVICE_LABELS.get(value, value.replace("_", " ").replace("-", " ").title())


def _category_label(value: str) -> str:
    return {
        "node": "Node",
        "python": "Python",
        "agent": "Agent",
        "config": "配置",
    }.get(value, value.replace("_", " "))


def _package_label(value: str) -> str:
    return _PACKAGE_LABELS.get(value, value.replace("-", " "))


def _task_device_label(object_id: str) -> str | None:
    if object_id.startswith("remote__"):
        parts = object_id.split("__")
        if len(parts) >= 2:
            return _device_label(parts[1])
    if object_id.startswith("local__"):
        return "当前 WSL"
    return None


def _task_target_label(object_id: str) -> str | None:
    parts = object_id.split("__")
    if object_id.startswith("remote__") and len(parts) >= 4:
        return _package_label(parts[3])
    if object_id.startswith("local__") and len(parts) >= 3:
        return _package_label(parts[2])
    return None


def _task_category_label(object_id: str) -> str | None:
    parts = object_id.split("__")
    if object_id.startswith("remote__") and len(parts) >= 3:
        return _category_label(parts[2])
    if object_id.startswith("local__") and len(parts) >= 2:
        return _category_label(parts[1])
    return None


def _command_label(command: list[str]) -> str:
    lower = [part.lower() for part in command]
    joined = " ".join(lower)
    if not command:
        return "执行命令"
    if "ssh" in lower:
        remote_command = lower[-1] if lower else ""
        if "npm install" in remote_command or "npm update" in remote_command:
            return "远程安装/更新 Node 工具"
        if "npm uninstall" in remote_command:
            return "远程删除 Node 工具"
        if "pip install" in remote_command:
            return "远程安装/更新 Python 包"
        if "pip uninstall" in remote_command:
            return "远程删除 Python 包"
        return "远程执行命令"
    if lower[0] == "docker":
        if "compose" in lower:
            if "pull" in lower:
                return "拉取最新镜像"
            if "build" in lower:
                return "构建容器镜像"
            if "up" in lower:
                return "部署并启动容器"
            if "stop" in lower:
                return "停止容器服务"
            if "rm" in lower:
                return "删除容器实例"
            if "down" in lower:
                return "停止并清理 Compose 项目"
        if len(lower) > 1 and lower[1] == "build":
            return "构建 Docker 镜像"
        if len(lower) > 1 and lower[1] == "update" and "--restart" in lower:
            return "设置容器开机策略"
        return "执行 Docker 操作"
    if lower[0] in {"systemctl", "sudo"} and "systemctl" in lower:
        if "disable" in lower and "--now" in lower:
            return "关闭系统服务并取消自启"
        if "enable" in lower:
            return "开启系统服务自启"
        if "restart" in lower:
            return "重启系统服务"
        if "start" in lower:
            return "启动系统服务"
        if "stop" in lower:
            return "停止系统服务"
        return "调整系统服务"
    if lower[0] == "git":
        if "fetch" in lower:
            return "拉取上游版本信息"
        if "worktree" in lower:
            return "准备目标版本源码"
        if "checkout" in lower or "reset" in lower:
            return "切换代码版本"
        return "执行 Git 操作"
    if lower[0] in {"npm", "pnpm", "yarn"}:
        if "install" in lower or "update" in lower:
            return "更新 Node 依赖"
        if "uninstall" in lower or "remove" in lower:
            return "卸载 Node 工具"
        return "执行 Node 包管理操作"
    if lower[0] in {"pip", "pip3"} or "pip" in lower[:3]:
        if "install" in lower:
            return "安装或更新 Python 包"
        if "uninstall" in lower:
            return "卸载 Python 包"
        return "执行 Python 包管理操作"
    if (
        lower[0] in {"python", "python3"}
        or lower[0].endswith("/python3")
        or lower[0].endswith("/python")
    ):
        if "app.services.notifications" in joined:
            return "发送微信通知"
        if "urllib.request" in joined or "healthcheck" in joined:
            return "检查服务访问状态"
        if "worktree" in joined or "refs/tags" in joined:
            return "准备目标版本源码"
        if "docker compose" in joined and "image:" in joined:
            return "按指定镜像部署"
        if "dockerfile" in joined or "apt-mirror" in joined:
            return "准备镜像构建环境"
        return "执行 Python 辅助脚本"
    if lower[0] == "bash":
        if "cftunnel" in joined or "cloudflare" in joined:
            return "生成或刷新 CF 临时链接"
        return "执行脚本"
    if lower[0] == "rm":
        return "删除本地文件或目录"
    if lower[0] in {"curl", "wget"}:
        return "检查网络接口"
    return "执行命令"


def _compact_command(command: list[str], *, limit: int = 180) -> str:
    if not command:
        return "无命令"
    if "ssh" in command:
        remote_target = next(
            (part for part in command if "@" in part and not part.startswith("-")), ""
        )
        remote_command = command[-1] if command else ""
        if remote_command and remote_command not in {"ssh", remote_target}:
            target_text = f" -> {remote_target}" if remote_target else ""
            if (
                "base64.b64decode" in remote_command
                or "python3 - <<'PY'" in remote_command
            ):
                return f"远程执行{target_text}：<受保护的配置写入脚本>"
            text = f"远程执行{target_text}：{remote_command}"
            return text if len(text) <= limit else text[: limit - 1] + "…"
    display = list(command)
    if "-c" in display:
        index = display.index("-c")
        if index + 1 < len(display):
            display[index + 1] = "<inline-script>"
    text = shlex.join(display)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _operation_sentence(task: TaskRecord, command: list[str]) -> str:
    label = _command_label(command)
    device = _task_device_label(task.object_id)
    target = _task_target_label(task.object_id)
    category = _task_category_label(task.object_id)
    version = task.requested_version or "latest"

    if category in {"Node", "Python"} and target:
        where = f"在 {device} 上" if device else ""
        if "删除" in label or "卸载" in label:
            return f"{where}删除 {target}".strip()
        if "安装" in label or "更新" in label:
            return f"{where}安装/更新 {target}，目标版本 {version}".strip()
    if device:
        return f"在 {device} 上执行：{label}"
    return label


def _workflow_summary(plan: ActionPlan) -> str:
    labels: list[str] = []
    for command in [*plan.commands, *plan.success_commands]:
        label = _command_label(command)
        if label not in labels:
            labels.append(label)
    return " -> ".join(labels[:8]) if labels else "执行任务"


def _is_noise_line(line: str) -> bool:
    clean = line.strip()
    lower = clean.lower()
    if not clean:
        return True
    if lower.startswith("npm timing "):
        return True
    if lower in {
        "npm warn cleanup [",
        "npm warn cleanup   [",
        "npm warn cleanup   ]",
        "npm warn cleanup ]",
    }:
        return True
    return False


def _humanize_log_line(line: str) -> str | None:
    clean = line.strip()
    if _is_noise_line(clean):
        return None
    lower = clean.lower()
    if "command not found: npm" in lower or clean in {
        "npm: command not found",
        "bash: npm: command not found",
    }:
        return "没有找到 npm：远程 shell 没加载 Node/nvm 环境"
    if "command not found: node" in lower or clean in {
        "node: command not found",
        "bash: node: command not found",
    }:
        return "没有找到 node：远程 shell 没加载 Node/nvm 环境"
    if "could not resolve hostname" in lower:
        return "SSH 目标地址解析失败：节点地址或测试配置不对"
    if "identity file" in lower and "not accessible" in lower:
        return "SSH Key 文件不存在或不可读"
    if "permission denied" in lower and "ssh" in lower:
        return "SSH 登录被拒绝：账号、密码或 Key 不可用"
    if "operation not permitted" in lower and "codex.exe" in lower:
        return "Windows 正在占用旧 codex.exe，npm 清理残留目录失败"
    if lower.startswith("worker failed: command"):
        return "执行命令失败：原始命令已保存到任务详情"
    if clean.startswith("changed ") and "packages in" in lower:
        return (
            clean.replace("changed", "已安装/更新")
            .replace("packages in", "个包，用时")
            .replace("package in", "个包，用时")
        )
    if clean.startswith("added ") and "packages in" in lower:
        return (
            clean.replace("added", "已新增")
            .replace("packages in", "个包，用时")
            .replace("package in", "个包，用时")
        )
    if clean.startswith("removed ") and "packages in" in lower:
        return (
            clean.replace("removed", "已删除")
            .replace("packages in", "个包，用时")
            .replace("package in", "个包，用时")
        )
    if lower.startswith("npm warn cleanup failed to remove some directories"):
        return "npm 清理旧目录失败；如果任务成功，通常不影响新版本安装"
    return clean


def _diagnose_text(text: str) -> str:
    lower = text.lower()
    if "command not found: npm" in lower:
        return "远程设备找不到 npm。常见原因是 nvm 只写在 ~/.zshrc，SSH 非交互命令没有加载它。"
    if "command not found: node" in lower:
        return "远程设备找不到 node。Node 环境没有进入 SSH 命令的 PATH。"
    if "could not resolve hostname" in lower:
        return "SSH 主机名解析失败。这个任务打到了无效节点或测试节点。"
    if "identity file" in lower and "not accessible" in lower:
        return "SSH Key 路径不存在。任务配置里的 key_path 不能访问。"
    if "operation not permitted" in lower and "codex.exe" in lower:
        return "Windows 上旧 codex.exe 被占用，npm 没能清理临时目录；任务返回成功时不影响新包落盘。"
    if "permission denied" in lower:
        return "权限不足或登录被拒绝。需要检查账号、密码、Key 或 npm 全局目录权限。"
    if "timed out" in lower or "timeout" in lower:
        return "执行超时。可能是网络慢、远程机器卡住，或命令等待输入。"
    return ""


def _suggestion_text(text: str) -> str:
    lower = text.lower()
    if "command not found: npm" in lower or "command not found: node" in lower:
        return (
            "把远程 Node 命令改成先加载 shell 环境，例如 source ~/.zshrc 后再执行 npm。"
        )
    if "could not resolve hostname" in lower or (
        "identity file" in lower and "not accessible" in lower
    ):
        return "删除或修正这个无效 SSH 节点，避免任务中心继续刷测试错误。"
    if "operation not permitted" in lower and "codex.exe" in lower:
        return "关闭 Windows 上正在运行的 Codex，再重新更新或手动清理 npm 临时目录。"
    if "permission denied" in lower:
        return "确认 SSH 登录方式和 npm 全局目录权限。"
    return ""


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines()) or 1


def _meaningful_lines(
    text: str, *, max_lines: int = 3, max_chars: int = 180
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in text.replace("\r", "\n").splitlines():
        line = _humanize_log_line(raw_line)
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    if not lines:
        return []
    selected = lines[:max_lines]
    return [
        line if len(line) <= max_chars else line[: max_chars - 1] + "…"
        for line in selected
    ]


def _append_output_summary(
    task: TaskRecord,
    chunk: str,
    *,
    sink: SystemTerminalSink,
    stream: Literal["stdout", "stderr"],
    failed: bool = True,
) -> None:
    lines = _meaningful_lines(chunk)
    total = _line_count(chunk)
    if not lines:
        return
    label = "报错" if stream == "stderr" and failed else "输出"
    for line in lines:
        append_system_chunk(sink, f"│  {label}：{line}\n")
    hidden = max(total - len(lines), 0)
    if hidden > 0:
        target = "stderr.log" if stream == "stderr" else "stdout.log"
        append_system_chunk(
            sink, f"│  {label}：还有 {hidden} 行已保存到任务详情的 {target}\n"
        )


def _duration_text(started: float) -> str:
    seconds = monotonic() - started
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{seconds / 60:.1f}min"


def _status_footer(task: TaskRecord, status: str) -> str:
    if not task.started_at or not task.finished_at:
        return status
    seconds = (task.finished_at - task.started_at).total_seconds()
    return f"{status} · 用时 {seconds:.1f}s"


def _run_subprocess_text(
    command: list[str],
    *,
    cwd: str | None,
    timeout: float | None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except TypeError as exc:
        message = str(exc)
        if "encoding" not in message and "errors" not in message:
            raise
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def _run_command_with_retry(
    task: TaskRecord,
    command: list[str],
    plan: ActionPlan,
    *,
    sink: SystemTerminalSink,
    step_index: int,
    step_total: int,
    post_success: bool = False,
) -> subprocess.CompletedProcess[str]:
    policy = plan.retry_policy or {}
    max_attempts = int(policy.get("max_attempts", 1) or 1)
    delay_seconds = float(policy.get("delay_seconds", 0) or 0)
    retry_on_stderr = (
        [str(item) for item in policy.get("retry_on_stderr", []) if str(item)]
        if isinstance(policy.get("retry_on_stderr"), list)
        else []
    )
    completed: subprocess.CompletedProcess[str] | None = None
    label = _command_label(command)
    prefix = "收尾" if post_success else "步骤"
    append_system_chunk(sink, f"├─ {prefix} {step_index}/{step_total}：{label}\n")
    append_system_chunk(sink, f"│  操作：{_operation_sentence(task, command)}\n")

    for attempt in range(1, max_attempts + 1):
        started = monotonic()
        if max_attempts > 1:
            append_system_chunk(sink, f"│  尝试：{attempt}/{max_attempts}\n")
        try:
            completed = _run_subprocess_text(
                command,
                cwd=plan.working_dir,
                timeout=plan.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_timeout_chunk(exc.output)
            stderr = _coerce_timeout_chunk(exc.stderr)
            if stdout:
                append_task_chunk(
                    task, stdout, sink=sink, stream="stdout", mirror_to_system=False
                )
                _append_output_summary(task, stdout, sink=sink, stream="stdout")
            timeout_message = (
                f"command timed out after {plan.command_timeout_seconds:g}s\n"
            )
            stderr_payload = f"{stderr}{timeout_message}"
            append_task_chunk(
                task, stderr_payload, sink=sink, stream="stderr", mirror_to_system=False
            )
            _append_output_summary(task, stderr_payload, sink=sink, stream="stderr")
            append_system_chunk(
                sink, f"│  结果：超时 · 超过 {plan.command_timeout_seconds:g}s\n"
            )
            return subprocess.CompletedProcess(
                command, 124, stdout=stdout, stderr=stderr_payload
            )
        if completed.stdout:
            append_task_chunk(
                task,
                completed.stdout,
                sink=sink,
                stream="stdout",
                mirror_to_system=False,
            )
            _append_output_summary(task, completed.stdout, sink=sink, stream="stdout")
        if completed.stderr:
            append_task_chunk(
                task,
                completed.stderr,
                sink=sink,
                stream="stderr",
                mirror_to_system=False,
            )
            _append_output_summary(task, completed.stderr, sink=sink, stream="stderr", failed=completed.returncode != 0)
        if completed.returncode == 0:
            diagnosis = _diagnose_text(completed.stderr or "")
            if diagnosis:
                append_system_chunk(sink, f"│  提醒：{diagnosis}\n")
            append_system_chunk(sink, f"│  完成：{label} · {_duration_text(started)}\n")
            return completed
        if attempt >= max_attempts or not _should_retry(completed, retry_on_stderr):
            diagnosis = _diagnose_text(
                (completed.stderr or "") + "\n" + (completed.stdout or "")
            )
            suggestion = _suggestion_text(
                (completed.stderr or "") + "\n" + (completed.stdout or "")
            )
            if diagnosis:
                append_system_chunk(sink, f"│  诊断：{diagnosis}\n")
            if suggestion:
                append_system_chunk(sink, f"│  建议：{suggestion}\n")
            append_system_chunk(
                sink, f"│  结果：失败 · 退出码 {completed.returncode}\n"
            )
            return completed
        append_system_chunk(
            sink,
            f"│  重试：第 {attempt}/{max_attempts} 次失败，退出码 {completed.returncode}，等待 {delay_seconds:g}s 后继续\n",
        )
        if delay_seconds > 0:
            sleep(delay_seconds)

    assert completed is not None
    return completed


def _should_retry(
    completed: subprocess.CompletedProcess[str], retry_on_stderr: list[str]
) -> bool:
    if completed.returncode == 0:
        return False
    stderr = (completed.stderr or "").lower()
    return any(marker.lower() in stderr for marker in retry_on_stderr)


def _coerce_timeout_chunk(chunk: str | bytes | None) -> str:
    if chunk is None:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode(errors="replace")
    return chunk


def execute_action_plan(
    task: TaskRecord,
    plan: ActionPlan,
    *,
    sink: SystemTerminalSink,
    store: TaskStore | None = None,
) -> None:
    task.status = "running"
    task.started_at = datetime.now(UTC)
    if store is not None:
        store.update(task)

    try:
        all_steps = len(plan.commands) + len(plan.success_commands)
        device = _task_device_label(task.object_id)
        category = _task_category_label(task.object_id)
        target = _task_target_label(task.object_id)
        context_lines = ""
        if device:
            context_lines += f"│  设备：{device}\n"
        if category:
            context_lines += f"│  类型：{category}\n"
        if target:
            context_lines += f"│  对象：{target}\n"
        append_system_chunk(
            sink,
            (
                "\n"
                f"┌─ 任务开始：{_friendly_action(task.action)}\n"
                f"│  项目：{_friendly_object(task.object_id)}\n"
                f"{context_lines}"
                f"│  版本：{task.requested_version or 'latest'}\n"
                f"│  流程：{_workflow_summary(plan)}\n"
                f"│  任务ID：{task.id}\n"
            ),
        )
        step_index = 0
        for command in plan.commands:
            step_index += 1
            completed = _run_command_with_retry(
                task,
                command,
                plan,
                sink=sink,
                step_index=step_index,
                step_total=all_steps,
            )
            if completed.returncode != 0:
                append_system_chunk(
                    sink,
                    f"└─ 结论：❌ 失败 · 退出码 {completed.returncode} · 完整报错已保存到任务详情\n",
                )
                raise subprocess.CalledProcessError(
                    completed.returncode,
                    command,
                    output=completed.stdout,
                    stderr=completed.stderr,
                )
        for command in plan.success_commands:
            step_index += 1
            success_plan = plan.model_copy(
                update={
                    "working_dir": plan.working_dir
                    if plan.working_dir and Path(plan.working_dir).exists()
                    else None
                }
            )
            completed = _run_command_with_retry(
                task,
                command,
                success_plan,
                sink=sink,
                step_index=step_index,
                step_total=all_steps,
                post_success=True,
            )
            if completed.returncode != 0:
                append_system_chunk(
                    sink,
                    f"└─ 结论：❌ 收尾失败 · 退出码 {completed.returncode} · 完整报错已保存到任务详情\n",
                )
                raise subprocess.CalledProcessError(
                    completed.returncode,
                    command,
                    output=completed.stdout,
                    stderr=completed.stderr,
                )
    except Exception:
        task.status = "failed"
        task.finished_at = datetime.now(UTC)
        if store is not None:
            store.update(task)
        raise

    task.status = "succeeded"
    task.finished_at = datetime.now(UTC)
    if store is not None:
        store.update(task)
    append_system_chunk(sink, f"└─ 结论：✅ {_status_footer(task, '成功')}\n")
