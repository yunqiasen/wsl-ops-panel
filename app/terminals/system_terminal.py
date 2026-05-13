import json
from collections.abc import Iterator
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from time import sleep


class SystemTerminalSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()
        self._listener_lock = Lock()
        self._listeners: list[Callable[[str], None]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def write(self, chunk: str) -> None:
        with self._lock:
            with self.path.open('a', encoding='utf-8') as fh:
                fh.write(chunk)
        self._notify(chunk)

    def add_listener(self, listener: Callable[[str], None]) -> None:
        with self._listener_lock:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[str], None]) -> None:
        with self._listener_lock:
            self._listeners = [item for item in self._listeners if item is not listener]

    def _notify(self, chunk: str) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)

        stale: list[Callable[[str], None]] = []
        for listener in listeners:
            try:
                listener(chunk)
            except RuntimeError:
                stale.append(listener)

        if stale:
            with self._listener_lock:
                self._listeners = [item for item in self._listeners if item not in stale]


def _parse_last_event_id(last_event_id: str | None) -> int:
    if last_event_id is None:
        return 0

    try:
        return max(int(last_event_id), 0)
    except ValueError:
        return 0


def _format_sse_event(*, chunk: str, offset: int) -> str:
    payload = json.dumps({'chunk': chunk}, ensure_ascii=False, separators=(',', ':'))
    return f'id: {offset}\ndata: {payload}\n\n'


def iter_sse_events(path: Path, *, last_event_id: str | None = None, poll_interval: float = 0.1) -> Iterator[str]:
    offset = _parse_last_event_id(last_event_id)

    while True:
        if path.exists():
            file_size = path.stat().st_size
            if file_size < offset:
                offset = 0

            with path.open('rb') as fh:
                fh.seek(offset)
                chunk = fh.read()
                next_offset = fh.tell()

            if chunk:
                yield _format_sse_event(chunk=chunk.decode('utf-8'), offset=next_offset)
                offset = next_offset
                continue

        sleep(poll_interval)
