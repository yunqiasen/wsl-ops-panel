from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.terminals.system_terminal import iter_sse_events

router = APIRouter(prefix='/api/terminals', tags=['terminals'])
SYSTEM_TERMINAL_LOG_PATH = Path('data/terminals/system.log')


@router.get('/system/stream')
def stream_system_terminal() -> StreamingResponse:
    return StreamingResponse(iter_sse_events(SYSTEM_TERMINAL_LOG_PATH), media_type='text/event-stream')
