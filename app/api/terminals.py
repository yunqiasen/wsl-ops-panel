from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.terminals.system_terminal import iter_sse_events

router = APIRouter(prefix='/api/terminals', tags=['terminals'])
SYSTEM_TERMINAL_LOG_PATH = Path('data/terminals/system.log')


@router.get('/system/stream')
def stream_system_terminal(request: Request) -> StreamingResponse:
    last_event_id = request.headers.get('last-event-id')
    return StreamingResponse(
        iter_sse_events(SYSTEM_TERMINAL_LOG_PATH, last_event_id=last_event_id),
        media_type='text/event-stream',
    )
