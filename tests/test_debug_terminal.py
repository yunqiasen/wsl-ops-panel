import json
from pathlib import Path
from queue import Queue
from threading import Thread
from time import sleep, time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import COOKIE_NAME, issue_session_token
from tests.app_factory import create_app
from app.terminals.debug_terminal import DebugTerminalManager


DEFAULT_CWD = '/home/div/1_Project_dir/AI'
MARKER = '__WSL_OPS_PANEL_DEBUG__'


def make_manager(tmp_path: Path) -> DebugTerminalManager:
    return DebugTerminalManager(base_dir=tmp_path / 'terminals', default_cwd=DEFAULT_CWD, default_shell='/bin/bash')


def receive_text_with_timeout(websocket, *, timeout: float = 1.0) -> str:
    result: Queue[tuple[str, str | BaseException]] = Queue(maxsize=1)

    def worker() -> None:
        try:
            result.put(('text', websocket.receive_text()))
        except BaseException as exc:  # pragma: no cover - exercised via caller assertions
            result.put(('error', exc))

    thread = Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f'timed out waiting for websocket output after {timeout} seconds')

    kind, value = result.get_nowait()
    if kind == 'error':
        raise value
    return value


def receive_until_marker(websocket, marker: str, attempts: int = 12, *, timeout: float = 1.0) -> str:
    chunks: list[str] = []
    for _ in range(attempts):
        chunk = receive_text_with_timeout(websocket, timeout=timeout)
        chunks.append(chunk)
        joined = ''.join(chunks)
        if marker in joined:
            return joined
    raise AssertionError(f'marker {marker!r} not found in websocket output: {"".join(chunks)!r}')


def wait_for_session_status(client: TestClient, session_id: str, status: str, *, timeout: float = 2.0) -> dict:
    deadline = time() + timeout
    while time() < deadline:
        response = client.get('/api/terminals/debug')
        response.raise_for_status()
        for session in response.json():
            if session['id'] == session_id and session['status'] == status:
                return session
        sleep(0.05)
    raise AssertionError(f'session {session_id!r} did not reach status {status!r}')


def test_create_list_and_delete_debug_terminal_session(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    create_response = client.post('/api/terminals/debug', json={})

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload['cwd'] == DEFAULT_CWD
    assert payload['shell'] == '/bin/bash'
    assert payload['title'] is None
    assert payload['status'] == 'running'

    session_id = payload['id']
    list_response = client.get('/api/terminals/debug')

    assert list_response.status_code == 200
    assert [item['id'] for item in list_response.json()] == ['system', session_id]

    delete_response = client.delete(f'/api/terminals/debug/{session_id}')

    assert delete_response.status_code == 204
    assert [item['id'] for item in client.get('/api/terminals/debug').json()] == ['system']

    metadata_path = Path(payload['metadata_path'])
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    assert metadata['status'] == 'closed'
    assert metadata['closed_at'] is not None


def test_debug_terminal_runtime_sets_truecolor_env(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    captured_env = {}

    class FakeProcess:
        pid = 12345

        def poll(self):
            return 0

        def wait(self):
            return 0

    def fake_popen(*_args, **kwargs):
        captured_env.update(kwargs['env'])
        return FakeProcess()

    monkeypatch.setattr('app.terminals.debug_terminal.subprocess.Popen', fake_popen)
    monkeypatch.setattr('app.terminals.debug_terminal.os.openpty', lambda: (1, 2))
    monkeypatch.setattr('app.terminals.debug_terminal.os.close', lambda _fd: None)
    monkeypatch.setattr('app.terminals.debug_terminal.os.read', lambda _fd, _size: b'')

    manager.create_session()

    assert captured_env['COLORTERM'] == 'truecolor'
    assert captured_env['LC_CTYPE'].endswith('UTF-8')


def test_terminal_upload_saves_file_and_returns_absolute_path(tmp_path: Path, monkeypatch) -> None:

    app = create_app()
    app.state.terminal_upload_root = tmp_path / 'uploads'

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.post('/api/terminals/uploads', files={'file': ('截图 1.png', b'PNGDATA', 'image/png')})

    assert response.status_code == 200
    payload = response.json()
    assert payload['filename'] == '截图_1.png'
    assert payload['size'] == 7
    assert payload['path'].startswith(str(tmp_path / 'uploads'))
    assert Path(payload['path']).read_bytes() == b'PNGDATA'


def test_system_terminal_session_is_readonly_log_and_cannot_be_deleted(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.get('/api/terminals/debug')

    assert response.status_code == 200
    payload = response.json()
    assert len(payload) == 1
    assert payload[0]['id'] == 'system'
    assert payload[0]['title'] == '系统日志'
    assert payload[0]['shell'] == 'readonly-log'
    assert payload[0]['status'] == 'running'

    delete_response = client.delete('/api/terminals/debug/system')

    assert delete_response.status_code == 400
    assert delete_response.json()['detail'] == 'system terminal cannot be closed'


def test_system_terminal_websocket_is_not_a_debug_shell(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    with client.websocket_connect('/api/terminals/debug/system/ws') as websocket:
        with pytest.raises(WebSocketDisconnect):
            receive_text_with_timeout(websocket, timeout=1.0)


def test_terminal_websocket_resize_message_is_not_written_to_shell_input(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    create_response = client.post('/api/terminals/debug', json={})
    session = create_response.json()
    session_id = session['id']

    with client.websocket_connect(f'/api/terminals/debug/{session_id}/ws') as websocket:
        websocket.send_text('{"type":"resize","cols":120,"rows":36}')
        websocket.send_text(f'printf "{MARKER}\\n"\n')
        output = receive_until_marker(websocket, MARKER)

    assert MARKER in output
    assert 'resize' not in Path(session['input_log_path']).read_text(encoding='utf-8')

    delete_response = client.delete(f'/api/terminals/debug/{session_id}')
    assert delete_response.status_code == 204


def test_terminal_websocket_resize_message_updates_pty_size(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    create_response = client.post('/api/terminals/debug', json={})
    session = create_response.json()
    session_id = session['id']

    with client.websocket_connect(f'/api/terminals/debug/{session_id}/ws') as websocket:
        websocket.send_text(json.dumps({'type': 'resize', 'cols': 132, 'rows': 33}))
        websocket.send_text('stty size\n')
        output = receive_until_marker(websocket, '33 132', attempts=20)

    assert '33 132' in output

    delete_response = client.delete(f'/api/terminals/debug/{session_id}')
    assert delete_response.status_code == 204


def test_debug_terminal_session_can_be_renamed(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    session = client.post('/api/terminals/debug', json={}).json()

    response = client.patch(f'/api/terminals/debug/{session["id"]}', json={'title': '维护窗口'})

    assert response.status_code == 200
    assert response.json()['title'] == '维护窗口'
    assert client.get('/api/terminals/debug').json()[1]['title'] == '维护窗口'


def test_system_terminal_cannot_be_renamed(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())

    response = client.patch('/api/terminals/debug/system', json={'title': 'x'})

    assert response.status_code == 400
    assert response.json()['detail'] == 'system terminal cannot be renamed'


def test_debug_terminal_websocket_executes_command_and_persists_logs(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    create_response = client.post('/api/terminals/debug', json={})
    session = create_response.json()
    session_id = session['id']

    with client.websocket_connect(f'/api/terminals/debug/{session_id}/ws') as websocket:
        websocket.send_text(f'printf "{MARKER}\\n"\n')
        output = receive_until_marker(websocket, MARKER)

    assert MARKER in output

    output_log_path = Path(session['output_log_path'])
    input_log_path = Path(session['input_log_path'])
    assert MARKER in output_log_path.read_text(encoding='utf-8')
    assert f'printf "{MARKER}\\n"\n' in input_log_path.read_text(encoding='utf-8')

    delete_response = client.delete(f'/api/terminals/debug/{session_id}')
    assert delete_response.status_code == 204


def test_closed_debug_terminal_reconnect_closes_after_backlog(tmp_path: Path, monkeypatch) -> None:

    manager = make_manager(tmp_path)
    app = create_app()
    app.state.debug_terminal_manager = manager

    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, issue_session_token())
    create_response = client.post('/api/terminals/debug', json={})
    session = create_response.json()
    session_id = session['id']

    with client.websocket_connect(f'/api/terminals/debug/{session_id}/ws') as websocket:
        websocket.send_text(f'printf "{MARKER}\n"\nexit\n')
        output = receive_until_marker(websocket, MARKER)

    assert MARKER in output
    wait_for_session_status(client, session_id, 'closed')

    with client.websocket_connect(f'/api/terminals/debug/{session_id}/ws') as websocket:
        backlog = receive_until_marker(websocket, MARKER)
        assert MARKER in backlog
        with pytest.raises(WebSocketDisconnect):
            receive_text_with_timeout(websocket, timeout=1.0)

    delete_response = client.delete(f'/api/terminals/debug/{session_id}')
    assert delete_response.status_code == 204
