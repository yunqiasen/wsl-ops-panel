import json
from pathlib import Path
from time import monotonic, sleep

from app.adapters.base import ActionPlan
from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import InMemoryTaskStore
from app.tasks.worker import SerialTaskWorker
from app.terminals.system_terminal import SystemTerminalSink


def test_worker_executes_next_queued_task(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store=store)
    sink = SystemTerminalSink(tmp_path / 'system.log')
    calls: list[str] = []

    def fake_execute(task, plan, *, sink, store):
        calls.append(task.id)
        task.status = 'succeeded'
        store.update(task)

    monkeypatch.setattr('app.tasks.worker.execute_action_plan', fake_execute)

    worker = SerialTaskWorker(queue=queue, sink=sink, poll_interval=0.01)
    worker.start()
    try:
        task = queue.enqueue('cpa', 'update_latest', plan=ActionPlan(commands=[['echo', 'ok']]))
        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            if store.get(task.id).status == 'succeeded':
                break
            sleep(0.02)
        assert store.get(task.id).status == 'succeeded'
        assert calls == [task.id]
    finally:
        worker.stop()


def test_worker_reads_plan_from_task_plan_path(tmp_path: Path) -> None:
    operation_dir = tmp_path / 'operations'
    operation_dir.mkdir()
    plan_path = operation_dir / 'plan.json'
    plan_path.write_text(
        json.dumps(
            {
                'commands': [['echo', 'ok']],
                'requires_sudo': False,
                'working_dir': None,
                'preview_paths': [],
                'preview_objects': [],
            }
        ),
        encoding='utf-8',
    )

    plan = SerialTaskWorker.load_plan(plan_path)

    assert plan.commands == [['echo', 'ok']]


def test_worker_preserves_submission_order(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store=store)
    sink = SystemTerminalSink(tmp_path / 'system.log')
    calls: list[str] = []

    def fake_execute(task, plan, *, sink, store):
        calls.append(task.object_id)
        task.status = 'succeeded'
        store.update(task)

    monkeypatch.setattr('app.tasks.worker.execute_action_plan', fake_execute)

    worker = SerialTaskWorker(queue=queue, sink=sink, poll_interval=0.01)
    worker.start()
    try:
        first = queue.enqueue('cpa', 'update_latest', plan=ActionPlan(commands=[['echo', 'first']]))
        second = queue.enqueue('sub2api', 'delete', plan=ActionPlan(commands=[['echo', 'second']]))

        deadline = monotonic() + 1.0
        while monotonic() < deadline:
            if store.get(first.id).status == 'succeeded' and store.get(second.id).status == 'succeeded':
                break
            sleep(0.02)

        assert calls == ['cpa', 'sub2api']
    finally:
        worker.stop()
