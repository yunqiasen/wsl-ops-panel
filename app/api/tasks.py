from pathlib import Path
import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.ui import TEMPLATES, build_page_context, page_login_redirect
from app.models.tasks import TaskRecord
from app.tasks.executor import (
    _command_label,
    _diagnose_text,
    _friendly_action,
    _friendly_object,
    _humanize_log_line,
    _suggestion_text,
    _task_category_label,
    _task_device_label,
    _task_target_label,
)

router = APIRouter(tags=['tasks'])


@router.get('/tasks', response_class=HTMLResponse, name='tasks_page')
def tasks_page(request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    visible_tasks = request.app.state.task_store.list_recent(limit=120)
    task_items = [_build_task_item(task) for task in visible_tasks]
    context = build_page_context(
        request,
        title='任务中心',
        active_page='tasks',
        tasks=visible_tasks,
        task_items=task_items,
        shown_task_count=len(visible_tasks),
        task_stats=request.app.state.task_store.count_statuses(),
    )
    return TEMPLATES.TemplateResponse(request, 'tasks.html', context)


@router.get('/logs', name='logs_page')
def logs_page(request: Request) -> RedirectResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect
    return RedirectResponse(url='/terminals', status_code=302)


def _build_task_item(task: TaskRecord) -> dict[str, object]:
    plan_text = _public_plan(task)
    stdout = _read_text_excerpt(Path(task.stdout_log_path), max_chars=6000)
    stderr = _read_text_excerpt(Path(task.stderr_log_path), max_chars=6000)
    plan_steps = _plan_steps(plan_text)
    combined_log = '\n'.join(part for part in [stderr, stdout] if part)
    diagnosis = _diagnose_text(combined_log)
    suggestion = _suggestion_text(combined_log)
    return {
        'task': task,
        'object_label': _friendly_object(task.object_id),
        'action_label': _friendly_action(task.action),
        'status_label': _status_label(task.status),
        'status_tone': _status_tone(task.status),
        'duration': _duration(task),
        'device_label': _task_device_label(task.object_id) or '本机',
        'category_label': _task_category_label(task.object_id) or '任务',
        'target_label': _task_target_label(task.object_id) or _friendly_object(task.object_id),
        'diagnosis': diagnosis or _default_diagnosis(task),
        'suggestion': suggestion,
        'plan': plan_text,
        'plan_steps': plan_steps,
        'plan_summary': ' → '.join(plan_steps[:5]) if plan_steps else '暂无执行计划',
        'stdout': stdout,
        'stderr': stderr,
        'stdout_summary': _summary_lines(stdout),
        'stderr_summary': _summary_lines(stderr),
        'stdout_line_count': _line_count(stdout),
        'stderr_line_count': _line_count(stderr),
    }


def _public_plan(task: TaskRecord) -> str:
    if not task.plan_path or not Path(task.plan_path).is_file():
        return ''
    content = Path(task.plan_path).read_text(encoding='utf-8', errors='replace')
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return '执行计划格式异常'
    if payload.get('provider_projection') or task.action.startswith('agent_provider_'):
        # Execution snapshots and shell payloads are private; expose step labels only.
        payload.pop('provider_projection', None)
        for key in ('commands', 'success_commands'):
            payload[key] = [[_command_label(command)] for command in payload.get(key, [])]
    return json.dumps(payload, ensure_ascii=False, indent=2)[-8000:]


def _read_text_excerpt(path: Path, *, max_chars: int = 4000) -> str:
    if max_chars <= 0:
        return ''
    # Four bytes per code point plus a partial leading character; bound I/O,
    # not just the rendered output. Seeking avoids reading historical logs.
    max_bytes = max_chars * 4 + 3
    try:
        with path.open('rb') as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - max_bytes))
            content = fh.read(max_bytes)
    except OSError:
        return ''
    return content.decode('utf-8', errors='replace')[-max_chars:]


def _plan_steps(plan_text: str) -> list[str]:
    if not plan_text.strip():
        return []
    try:
        payload = json.loads(plan_text)
    except json.JSONDecodeError:
        return []
    steps: list[str] = []
    for command in [*(payload.get('commands') or []), *(payload.get('success_commands') or [])]:
        if isinstance(command, list):
            label = _command_label([str(item) for item in command])
            if label not in steps:
                steps.append(label)
    return steps


def _summary_lines(text: str, *, max_lines: int = 4, max_chars: int = 220) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in text.replace('\r', '\n').splitlines():
        line = _humanize_log_line(raw_line)
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    selected = lines[:max_lines]
    return [line if len(line) <= max_chars else line[: max_chars - 1] + '…' for line in selected]


def _default_diagnosis(task: TaskRecord) -> str:
    if task.status == 'succeeded':
        return '任务已执行完成，没有检测到阻断错误。'
    if task.status == 'failed':
        return '任务失败，但日志里没有命中已知错误模式；需要展开原始日志看细节。'
    if task.status == 'running':
        return '任务正在运行，等待后续输出。'
    if task.status == 'queued':
        return '任务还在队列中，尚未开始执行。'
    return '任务已结束。'


def _line_count(text: str) -> int:
    return len([line for line in text.replace('\r', '\n').splitlines() if line.strip()])


def _duration(task: TaskRecord) -> str:
    if task.started_at is None:
        return '未开始'
    end = task.finished_at or task.started_at
    seconds = max((end - task.started_at).total_seconds(), 0)
    if task.finished_at is None:
        return '运行中'
    if seconds < 1:
        return f'{seconds * 1000:.0f}ms'
    if seconds < 60:
        return f'{seconds:.1f}s'
    return f'{seconds / 60:.1f}min'


def _status_label(status: str) -> str:
    return {
        'queued': '排队中',
        'running': '运行中',
        'succeeded': '成功',
        'failed': '失败',
        'interrupted': '中断',
    }.get(status, status)


def _status_tone(status: str) -> str:
    return {
        'queued': 'warning',
        'running': 'warning',
        'succeeded': 'ok',
        'failed': 'danger',
        'interrupted': 'muted',
    }.get(status, 'muted')
