import json
import os
from pathlib import Path
from datetime import UTC, datetime
import subprocess
from time import monotonic, sleep

from app.adapters.base import ActionPlan
from app.models.tasks import TaskRecord
from app.tasks.executor import execute_action_plan
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


def test_worker_runs_success_hook_after_full_delete(tmp_path: Path, monkeypatch) -> None:
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store=store)
    sink = SystemTerminalSink(tmp_path / 'system.log')
    marker = tmp_path / 'deleted.marker'

    task = queue.enqueue(
        'aiclient2api',
        'full_delete',
        plan=ActionPlan(
            commands=[['true']],
            success_commands=[['python3', '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("ok", encoding="utf-8")', str(marker)]],
        ),
    )
    worker = SerialTaskWorker(queue=queue, sink=sink, poll_interval=0.01)
    worker._execute_task(task)

    assert store.get(task.id).status == 'succeeded'
    assert marker.read_text(encoding='utf-8') == 'ok'


def test_success_hook_runs_after_working_dir_is_deleted(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    queue = GlobalTaskQueue(store=store)
    sink = SystemTerminalSink(tmp_path / 'system.log')
    working_dir = tmp_path / 'removed-project'
    working_dir.mkdir()
    marker = tmp_path / 'deleted.marker'

    task = queue.enqueue(
        'demo',
        'full_delete',
        plan=ActionPlan(
            commands=[['python3', '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).rmdir()', str(working_dir)]],
            success_commands=[
                [
                    'python3',
                    '-c',
                    'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("ok", encoding="utf-8")',
                    str(marker),
                ]
            ],
            working_dir=str(working_dir),
        ),
    )
    worker = SerialTaskWorker(queue=queue, sink=sink, poll_interval=0.01)
    worker._execute_task(task)

    assert store.get(task.id).status == 'succeeded'
    assert marker.read_text(encoding='utf-8') == 'ok'




def test_execute_action_plan_retries_connection_reset_case_insensitively(tmp_path: Path, monkeypatch) -> None:
    calls = {'count': 0}

    def fake_run(command, *, cwd=None, check=False, capture_output=True, text=True, timeout=None):
        calls['count'] += 1
        if calls['count'] == 1:
            return subprocess.CompletedProcess(
                command,
                1,
                stdout='',
                stderr='ConnectionResetError: [Errno 104] Connection reset by peer',
            )
        return subprocess.CompletedProcess(command, 0, stdout='ok', stderr='')

    monkeypatch.setattr('app.tasks.executor.subprocess.run', fake_run)
    monkeypatch.setattr('app.tasks.executor.sleep', lambda seconds: None)
    task = TaskRecord(
        id='retry-connection-reset',
        object_id='sub2api',
        action='update_latest',
        status='queued',
        stdout_log_path=str(tmp_path / 'stdout.log'),
        stderr_log_path=str(tmp_path / 'stderr.log'),
        plan_path=str(tmp_path / 'plan.json'),
        created_at=datetime.now(UTC),
    )
    sink = SystemTerminalSink(tmp_path / 'system.log')

    execute_action_plan(
        task,
        ActionPlan(
            commands=[['python3', '-c', 'healthcheck']],
            retry_policy={'max_attempts': 2, 'delay_seconds': 0.1, 'retry_on_stderr': ['connection reset by peer']},
        ),
        sink=sink,
    )

    assert task.status == 'succeeded'
    assert calls['count'] == 2

def test_execute_action_plan_retries_transient_registry_eof(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime
    import subprocess

    from app.models.tasks import TaskRecord
    from app.tasks.executor import execute_action_plan

    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check, capture_output, text, timeout=None):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='failed to do request: EOF\n')
        return subprocess.CompletedProcess(command, 0, stdout='ok\n', stderr='')

    monkeypatch.setattr('app.tasks.executor.subprocess.run', fake_run)

    store = InMemoryTaskStore()
    task = TaskRecord(
        id='retry-task',
        object_id='sub2api',
        action='update_latest',
        status='queued',
        stdout_log_path=str(tmp_path / 'stdout.log'),
        stderr_log_path=str(tmp_path / 'stderr.log'),
        created_at=datetime.now(UTC),
    )
    store.insert(task)
    sink = SystemTerminalSink(tmp_path / 'system.log')

    execute_action_plan(
        task,
        ActionPlan(
            commands=[['docker', 'compose', 'pull', 'sub2api']],
            retry_policy={'max_attempts': 2, 'delay_seconds': 0.1, 'retry_on_stderr': ['EOF']},
        ),
        sink=sink,
        store=store,
    )

    assert len(calls) == 2
    assert store.get('retry-task').status == 'succeeded'
    assert '重试：第 1/2 次失败，退出码 1' in (tmp_path / 'system.log').read_text(encoding='utf-8')
    assert 'failed to do request: EOF' in (tmp_path / 'stderr.log').read_text(encoding='utf-8')



def test_execute_action_plan_applies_command_timeout(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, *, cwd=None, check=False, capture_output=True, text=True, timeout=None):
        captured['timeout'] = timeout
        return subprocess.CompletedProcess(command, 0, stdout='ok', stderr='')

    monkeypatch.setattr('app.tasks.executor.subprocess.run', fake_run)
    task = TaskRecord(
        id='timeout-task',
        object_id='openai_cpa',
        action='update_latest',
        status='queued',
        stdout_log_path=str(tmp_path / 'stdout.log'),
        stderr_log_path=str(tmp_path / 'stderr.log'),
        created_at=datetime.now(UTC),
    )
    sink = SystemTerminalSink(tmp_path / 'system.log')

    execute_action_plan(task, ActionPlan(commands=[['docker', 'build', '.']], command_timeout_seconds=123), sink=sink)

    assert captured['timeout'] == 123
    assert task.status == 'succeeded'


def test_execute_action_plan_marks_timeout_as_failed(tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, *, cwd=None, check=False, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(command, timeout=timeout, output='partial', stderr='hung')

    monkeypatch.setattr('app.tasks.executor.subprocess.run', fake_run)
    store = InMemoryTaskStore()
    task = TaskRecord(
        id='timeout-failed-task',
        object_id='openai_cpa',
        action='update_latest',
        status='queued',
        stdout_log_path=str(tmp_path / 'stdout.log'),
        stderr_log_path=str(tmp_path / 'stderr.log'),
        created_at=datetime.now(UTC),
    )
    store.insert(task)
    sink = SystemTerminalSink(tmp_path / 'system.log')

    try:
        execute_action_plan(task, ActionPlan(commands=[['docker', 'build', '.']], command_timeout_seconds=123), sink=sink, store=store)
    except subprocess.CalledProcessError as exc:
        assert exc.returncode == 124
    else:
        raise AssertionError('expected timeout failure')

    assert store.get('timeout-failed-task').status == 'failed'
    assert 'timed out after 123s' in (tmp_path / 'stderr.log').read_text(encoding='utf-8')


def test_task_queue_creates_private_operation_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    queue = GlobalTaskQueue(store=InMemoryTaskStore())

    previous_umask = os.umask(0)
    try:
        task = queue.enqueue(
            'demo',
            'update_latest',
            plan=ActionPlan(commands=[['echo', 'ok']]),
        )
    finally:
        os.umask(previous_umask)

    operation_dir = Path(task.plan_path).parent
    assert operation_dir.stat().st_mode & 0o777 == 0o700
    assert Path(task.plan_path).stat().st_mode & 0o777 == 0o600


def test_compact_command_hides_remote_agent_configuration_payload() -> None:
    from app.tasks.executor import _compact_command

    command = [
        'ssh',
        'demo@example.test',
        "python3 - <<'PY'\nimport base64\nbase64.b64decode('secret-payload')\nPY",
    ]

    summary = _compact_command(command)

    assert summary == '远程执行 -> demo@example.test：<受保护的配置写入脚本>'
    assert 'secret-payload' not in summary
