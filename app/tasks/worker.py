import json
from pathlib import Path
from threading import Event, Thread

from app.adapters.base import ActionPlan
from app.models.tasks import TaskRecord
from app.tasks.executor import append_task_chunk, execute_action_plan
from app.tasks.queue import GlobalTaskQueue
from app.terminals.system_terminal import SystemTerminalSink


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

    def wake(self) -> None:
        self._wake_event.set()

    @staticmethod
    def load_plan(path: Path) -> ActionPlan:
        payload = json.loads(path.read_text(encoding='utf-8'))
        return ActionPlan.model_validate(payload)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            task = self.queue.peek_next_queued_task()
            if task is None:
                self._wake_event.wait(self.poll_interval)
                self._wake_event.clear()
                continue
            self._execute_task(task)

    def _execute_task(self, task: TaskRecord) -> None:
        if task.plan_path is None:
            append_task_chunk(task, 'missing plan_path\n', sink=self.sink, stream='stderr')
            task.status = 'failed'
            self.queue.store.update(task)
            return

        plan = self.load_plan(Path(task.plan_path))
        try:
            execute_action_plan(task, plan, sink=self.sink, store=self.queue.store)
        except Exception as exc:
            append_task_chunk(task, f'worker failed: {exc}\n', sink=self.sink, stream='stderr')
