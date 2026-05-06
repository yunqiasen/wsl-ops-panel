import asyncio
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.core.security import is_authenticated_websocket, require_authenticated_request
from app.models.terminals import DebugTerminalCreateRequest, DebugTerminalSession
from app.terminals.debug_terminal import DebugTerminalManager
from app.terminals.system_terminal import iter_sse_events

router = APIRouter(prefix='/api/terminals', tags=['terminals'])
SYSTEM_TERMINAL_LOG_PATH = Path('data/terminals/system.log')
debug_terminal_manager = DebugTerminalManager()


@router.get('/system/stream')
def stream_system_terminal(request: Request) -> StreamingResponse:
    require_authenticated_request(request)
    last_event_id = request.headers.get('last-event-id')
    return StreamingResponse(
        iter_sse_events(SYSTEM_TERMINAL_LOG_PATH, last_event_id=last_event_id),
        media_type='text/event-stream',
    )


@router.post('/debug', status_code=201, response_model=DebugTerminalSession)
def create_debug_terminal(request: Request, payload: DebugTerminalCreateRequest | None = Body(default=None)) -> DebugTerminalSession:
    require_authenticated_request(request)
    try:
        return debug_terminal_manager.create_session(
            shell=payload.shell if payload else None,
            cwd=payload.cwd if payload else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get('/debug', response_model=list[DebugTerminalSession])
def list_debug_terminals(request: Request) -> list[DebugTerminalSession]:
    require_authenticated_request(request)
    return debug_terminal_manager.list_sessions()


@router.delete('/debug/{session_id}', status_code=204)
def delete_debug_terminal(session_id: str, request: Request) -> Response:
    require_authenticated_request(request)
    if not debug_terminal_manager.close_session(session_id):
        raise HTTPException(status_code=404, detail='debug terminal session not found')
    return Response(status_code=204)


@router.websocket('/debug/{session_id}/ws')
async def debug_terminal_ws(websocket: WebSocket, session_id: str) -> None:
    if not is_authenticated_websocket(websocket):
        await websocket.close(code=4401)
        return

    runtime = debug_terminal_manager.get_runtime(session_id)
    await websocket.accept()
    if runtime is None:
        await websocket.close(code=4404)
        return

    backlog, queue = runtime.attach(asyncio.get_running_loop())
    if backlog:
        await websocket.send_text(backlog)
    if runtime.is_closed():
        runtime.unsubscribe(queue)
        await websocket.close()
        return

    sender = asyncio.create_task(_pump_terminal_output(websocket, queue))

    try:
        while True:
            try:
                chunk = await asyncio.wait_for(websocket.receive_text(), timeout=0.2)
            except TimeoutError:
                if runtime.is_closed() and sender.done():
                    break
                continue
            try:
                runtime.write_input(chunk)
            except RuntimeError:
                break
    except WebSocketDisconnect:
        pass
    finally:
        runtime.unsubscribe(queue)
        sender.cancel()
        with suppress(asyncio.CancelledError):
            await sender
        with suppress(RuntimeError):
            await websocket.close()


async def _pump_terminal_output(websocket: WebSocket, queue: asyncio.Queue[str | None]) -> None:
    while True:
        chunk = await queue.get()
        if chunk is None:
            return
        await websocket.send_text(chunk)
