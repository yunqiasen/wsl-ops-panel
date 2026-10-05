from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from threading import Event, Thread

from app.adapters.base import ActionPlan
from app.models.tasks import TaskRecord
from app.tasks.executor import append_system_chunk, append_task_chunk, execute_action_plan
from app.tasks.queue import GlobalTaskQueue
from app.terminals.system_terminal import SystemTerminalSink


LOGGER = logging.getLogger(__name__)


class SerialTaskWorker:
    def __init__(
        self,
        *,
        queue: GlobalTaskQueue,
        sink: SystemTerminalSink,
        poll_interval: float = 0.2,
    ) -> None:
        self.queue = queue
        self.sink = sink
        self.poll_interval = poll_interval
        self._wake_event = Event()
        self._stop_event = Event()
        self._thread: Thread | None = None
        self.queue.set_enqueue_notifier(self.wake)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name='wsl-ops-task-runner', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def wake(self) -> None:
        self._wake_event.set()

    @staticmethod
    def load_plan(path: Path) -> ActionPlan:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return ActionPlan.model_validate(payload)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self.queue.peek_next_queued_task()
                if task is not None:
                    self._execute_task(task)
                    continue
            except Exception as exc:
                # Store reads/writes can fail transiently; keep the consumer alive.
                LOGGER.warning('task worker poll failed (%s)', type(exc).__name__)
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()

    def _execute_task(self, task: TaskRecord) -> None:
        try:
            if task.plan_path is None:
                raise ValueError('missing plan_path')
            plan = self.load_plan(Path(task.plan_path))
            execute_action_plan(task, plan, sink=self.sink, store=self.queue.store)
        except Exception as exc:
            # Settle before logging: a broken sink must not strand a queued task.
            if task.status != 'succeeded':
                task.started_at = task.started_at or datetime.now(UTC)
                task.status = 'failed'
                task.finished_at = datetime.now(UTC)
                self.queue.store.update(task)
            # Plan validation can contain credentials; do not echo its payload.
            message = f'worker failed: {type(exc).__name__}; task={task.id}\n'
            try:
                append_task_chunk(task, message, sink=self.sink, stream='stderr', mirror_to_system=False)
                append_system_chunk(self.sink, f'└─ 任务异常：{type(exc).__name__} · {task.id}\n')
            except Exception:
                LOGGER.warning('task %s settled as %s; diagnostic log unavailable', task.id, task.status)
