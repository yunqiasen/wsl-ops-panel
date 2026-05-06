import shlex
import subprocess
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
    stream: Literal['stdout', 'stderr'] = 'stdout',
) -> None:
    sink.write(f'[{task.object_id}] {chunk}')
    log_path = Path(task.stdout_log_path if stream == 'stdout' else task.stderr_log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a', encoding='utf-8') as fh:
        fh.write(chunk)


def execute_action_plan(
    task: TaskRecord,
    plan: ActionPlan,
    *,
    sink: SystemTerminalSink,
    store: TaskStore | None = None,
) -> None:
    task.status = 'running'
    task.started_at = datetime.now(UTC)
    if store is not None:
        store.update(task)

    try:
        for command in plan.commands:
            append_task_chunk(task, f'$ {shlex.join(command)}\n', sink=sink)
            completed = subprocess.run(
                command,
                cwd=plan.working_dir,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.stdout:
                append_task_chunk(task, completed.stdout, sink=sink, stream='stdout')
            if completed.stderr:
                append_task_chunk(task, completed.stderr, sink=sink, stream='stderr')
            if completed.returncode != 0:
                raise subprocess.CalledProcessError(
                    completed.returncode,
                    command,
                    output=completed.stdout,
                    stderr=completed.stderr,
                )
    except Exception:
        task.status = 'failed'
        task.finished_at = datetime.now(UTC)
        if store is not None:
            store.update(task)
        raise

    task.status = 'succeeded'
    task.finished_at = datetime.now(UTC)
    if store is not None:
        store.update(task)
