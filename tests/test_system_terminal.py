from datetime import datetime
from pathlib import Path

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.api import terminals as terminals_api
from app.main import create_app
from app.models.tasks import TaskRecord
from app.tasks.executor import append_task_chunk
from app.terminals.system_terminal import SystemTerminalSink


def make_task(tmp_path: Path) -> TaskRecord:
    return TaskRecord(
        id='task-1',
        object_id='object-1',
        action='deploy',
        status='running',
        stdout_log_path=str(tmp_path / 'stdout.log'),
        stderr_log_path=str(tmp_path / 'stderr.log'),
        created_at=datetime(2026, 5, 4, 0, 0, 0),
    )


def test_system_terminal_sink_is_append_only(tmp_path: Path) -> None:
    sink = SystemTerminalSink(tmp_path / 'system.log')

    sink.write('hello\n')
    sink.write('world\n')

    assert (tmp_path / 'system.log').read_text(encoding='utf-8') == 'hello\nworld\n'


def test_system_terminal_stream_returns_sse_with_log_contents(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / 'system.log'
    log_path.write_text('boot ok\nsecond line\n', encoding='utf-8')
    monkeypatch.setattr(terminals_api, 'SYSTEM_TERMINAL_LOG_PATH', log_path)

    route_response = terminals_api.stream_system_terminal()
    assert isinstance(route_response, StreamingResponse)

    client = TestClient(create_app())
    response = client.get('/api/terminals/system/stream')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert 'data: boot ok\n\n' in response.text
    assert 'data: second line\n\n' in response.text


def test_append_task_chunk_writes_sink_and_stream_logs(tmp_path: Path) -> None:
    task = make_task(tmp_path)
    sink_path = tmp_path / 'system.log'
    sink = SystemTerminalSink(sink_path)

    append_task_chunk(task, 'stdout line\n', sink=sink, stream='stdout')
    append_task_chunk(task, 'stderr line\n', sink=sink, stream='stderr')

    assert sink_path.read_text(encoding='utf-8') == (
        '[object-1] stdout line\n'
        '[object-1] stderr line\n'
    )
    assert Path(task.stdout_log_path).read_text(encoding='utf-8') == 'stdout line\n'
    assert Path(task.stderr_log_path).read_text(encoding='utf-8') == 'stderr line\n'
