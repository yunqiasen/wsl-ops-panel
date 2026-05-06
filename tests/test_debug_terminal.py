import json
from pathlib import Path
from queue import Queue
from threading import Thread
from time import sleep, time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.security import COOKIE_NAME, issue_session_token
from app.main import create_app
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
    from app.api import terminals as terminals_api

    manager = make_manager(tmp_path)
    monkeypatch.setattr(terminals_api, 'debug_terminal_manager', manager)

    client = TestClient(create_app())
    client.cookies.set(COOKIE_NAME, issue_session_token())

    create_response = client.post('/api/terminals/debug', json={})

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload['cwd'] == DEFAULT_CWD
    assert payload['shell'] == '/bin/bash'
    assert payload['status'] == 'running'

    session_id = payload['id']
    list_response = client.get('/api/terminals/debug')

    assert list_response.status_code == 200
    assert [item['id'] for item in list_response.json()] == [session_id]

    delete_response = client.delete(f'/api/terminals/debug/{session_id}')

    assert delete_response.status_code == 204
    assert client.get('/api/terminals/debug').json() == []

    metadata_path = Path(payload['metadata_path'])
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    assert metadata['status'] == 'closed'
    assert metadata['closed_at'] is not None


def test_debug_terminal_websocket_executes_command_and_persists_logs(tmp_path: Path, monkeypatch) -> None:
    from app.api import terminals as terminals_api

    manager = make_manager(tmp_path)
    monkeypatch.setattr(terminals_api, 'debug_terminal_manager', manager)

    client = TestClient(create_app())
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
    from app.api import terminals as terminals_api

    manager = make_manager(tmp_path)
    monkeypatch.setattr(terminals_api, 'debug_terminal_manager', manager)

    client = TestClient(create_app())
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
