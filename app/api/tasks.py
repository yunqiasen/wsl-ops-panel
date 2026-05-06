from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.ui import TEMPLATES, build_page_context, page_login_redirect

router = APIRouter(tags=['tasks'])


@router.get('/tasks', response_class=HTMLResponse, name='tasks_page')
def tasks_page(request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    tasks = list(reversed(request.app.state.task_store.list_all()))
    context = build_page_context(
        request,
        title='任务中心',
        active_page='tasks',
        tasks=tasks,
    )
    return TEMPLATES.TemplateResponse(request, 'tasks.html', context)


@router.get('/logs', response_class=HTMLResponse, name='logs_page')
def logs_page(request: Request) -> HTMLResponse:
    redirect = page_login_redirect(request)
    if redirect is not None:
        return redirect

    tasks = list(reversed(request.app.state.task_store.list_all()))[:5]
    system_log = _read_text_excerpt(Path('data/terminals/system.log'))
    task_logs = [
        {
            'task': task,
            'stdout': _read_text_excerpt(Path(task.stdout_log_path)),
            'stderr': _read_text_excerpt(Path(task.stderr_log_path)),
        }
        for task in tasks
    ]
    context = build_page_context(
        request,
        title='日志中心',
        active_page='logs',
        system_log=system_log,
        task_logs=task_logs,
    )
    return TEMPLATES.TemplateResponse(request, 'logs.html', context)


def _read_text_excerpt(path: Path, *, max_chars: int = 4000) -> str:
    if not path.exists():
        return ''
    content = path.read_text(encoding='utf-8', errors='replace')
    return content[-max_chars:]
