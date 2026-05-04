from pathlib import Path
from typing import Literal

from app.models.tasks import TaskRecord
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
