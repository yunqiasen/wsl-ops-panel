from datetime import datetime
from pathlib import Path
from threading import Thread
from time import sleep

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.tasks import TaskRecord
from app.tasks.executor import append_task_chunk
from app.terminals.system_terminal import SystemTerminalSink, iter_sse_events


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


def test_terminals_page_renders_template() -> None:
    client = TestClient(create_app())

    response = client.get('/terminals')

    assert response.status_code == 200
    assert 'System Terminal' in response.text


def test_system_terminal_stream_route_returns_sse_content(tmp_path: Path, monkeypatch) -> None:
    from app.api import terminals as terminals_api

    log_path = tmp_path / 'system.log'
    log_path.write_text('boot ok\n', encoding='utf-8')
    monkeypatch.setattr(terminals_api, 'SYSTEM_TERMINAL_LOG_PATH', log_path)

    original_iter = terminals_api.iter_sse_events

    def one_event_stream(path: Path, *, last_event_id: str | None = None):
        events = original_iter(path, last_event_id=last_event_id, poll_interval=0.01)
        try:
            yield next(events)
        finally:
            events.close()

    monkeypatch.setattr(terminals_api, 'iter_sse_events', one_event_stream)

    client = TestClient(create_app())
    response = client.get('/api/terminals/system/stream')

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert 'data: boot ok' in response.text


def test_iter_sse_events_emits_existing_and_appended_lines(tmp_path: Path) -> None:
    log_path = tmp_path / 'system.log'
    log_path.write_text('boot ok\n', encoding='utf-8')
    events = iter_sse_events(log_path, poll_interval=0.01)

    assert next(events) == 'id: 0\ndata: boot ok\n\n'

    writer = Thread(
        target=lambda: (sleep(0.03), log_path.open('a', encoding='utf-8').write('second line\n')),
        daemon=True,
    )
    writer.start()

    assert next(events) == 'id: 1\ndata: second line\n\n'
    events.close()
    writer.join(timeout=0.2)


def test_iter_sse_events_resumes_from_last_event_id(tmp_path: Path) -> None:
    log_path = tmp_path / 'system.log'
    log_path.write_text('first\nsecond\nthird\n', encoding='utf-8')

    events = iter_sse_events(log_path, last_event_id='1', poll_interval=0.01)

    assert next(events) == 'id: 2\ndata: third\n\n'
    events.close()


def test_iter_sse_events_preserves_trailing_spaces_and_tabs(tmp_path: Path) -> None:
    log_path = tmp_path / 'system.log'
    log_path.write_text('keep space   \nkeep tab\t\n', encoding='utf-8')

    events = iter_sse_events(log_path, poll_interval=0.01)

    assert next(events) == 'id: 0\ndata: keep space   \n\n'
    assert next(events) == 'id: 1\ndata: keep tab\t\n\n'
    events.close()


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
