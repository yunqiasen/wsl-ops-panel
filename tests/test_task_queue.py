from pathlib import Path

from app.tasks.queue import GlobalTaskQueue
from app.tasks.store import InMemoryTaskStore, SQLiteTaskStore


def test_queue_runs_tasks_in_submission_order() -> None:
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store=store)
    first = queue.enqueue('cpa', 'update_latest')
    second = queue.enqueue('sub2api', 'delete')
    assert [task.id for task in store.list_all()] == [first.id, second.id]


def test_mark_running_tasks_interrupted_on_boot() -> None:
    store = InMemoryTaskStore(seed_running=True)
    queue = GlobalTaskQueue(store=store)
    queue.recover_on_startup()
    assert store.list_all()[0].status == 'interrupted'


def test_sqlite_store_persists_tasks_in_submission_order(tmp_path: Path) -> None:
    store = SQLiteTaskStore(tmp_path / 'tasks.db')
    queue = GlobalTaskQueue(store=store)

    first = queue.enqueue('cpa', 'update_latest')
    second = queue.enqueue('sub2api', 'delete', requested_version='2026.05')

    reloaded = SQLiteTaskStore(tmp_path / 'tasks.db')
    tasks = reloaded.list_all()

    assert [task.id for task in tasks] == [first.id, second.id]
    assert tasks[1].requested_version == '2026.05'



def test_queue_persists_unique_log_paths_and_plan_snapshot(tmp_path: Path) -> None:
    from app.adapters.base import ActionPlan

    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store=store)
    plan = ActionPlan(commands=[['echo', 'ok']], working_dir=str(tmp_path))

    task = queue.enqueue('cpa', 'update_latest', plan=plan)

    assert task.stdout_log_path.endswith('/stdout.log')
    assert task.stderr_log_path.endswith('/stderr.log')
    assert task.plan_path is not None
    assert Path(task.plan_path).exists()
