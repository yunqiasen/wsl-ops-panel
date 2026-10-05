import json
import codecs
import os
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
        self.path.chmod(0o600)

    def write(self, chunk: str) -> None:
        with self._lock:
            with self.path.open('a', encoding='utf-8') as fh:
                fh.write(chunk)
            self.path.chmod(0o600)
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


def iter_sse_events(
    path: Path,
    *,
    last_event_id: str | None = None,
    poll_interval: float = 0.1,
    idle_heartbeat: int = 150,
    initial_tail_bytes: int = 200_000,
) -> Iterator[str]:
    offset = _parse_last_event_id(last_event_id)
    if last_event_id is None and path.exists():
        offset = max(path.stat().st_size - initial_tail_bytes, 0)
    idle_ticks = 0
    identity = None
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    while True:
        try:
            fh = path.open('rb')
        except FileNotFoundError:
            fh = None
        if fh is not None:
            with fh:
                stat = os.fstat(fh.fileno())
                current_identity = (stat.st_dev, stat.st_ino)
                if (identity is not None and identity != current_identity) or stat.st_size < offset:
                    offset = 0
                    decoder.reset()
                identity = current_identity
                fh.seek(offset)
                chunk = fh.read()
                next_offset = fh.tell()
            if chunk:
                text = decoder.decode(chunk, final=False)
                offset = next_offset
                # SSE IDs acknowledge only complete characters, so reconnects can
                # replay a partial trailing character without loss.
                acknowledged = offset - len(decoder.getstate()[0])
                idle_ticks = 0
                if text:
                    yield _format_sse_event(chunk=text, offset=acknowledged)
                continue

        idle_ticks += 1
        if idle_ticks >= idle_heartbeat:
            idle_ticks = 0
            yield ': keepalive\n\n'
        sleep(poll_interval)
