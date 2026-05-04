from collections.abc import Iterator
from pathlib import Path
from time import sleep


class SystemTerminalSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def write(self, chunk: str) -> None:
        with self.path.open('a', encoding='utf-8') as fh:
            fh.write(chunk)


def _parse_last_event_id(last_event_id: str | None) -> int:
    if last_event_id is None:
        return 0

    try:
        return max(int(last_event_id) + 1, 0)
    except ValueError:
        return 0


def _strip_line_ending(line: str) -> str:
    if line.endswith('\r\n'):
        return line[:-2]
    if line.endswith('\n') or line.endswith('\r'):
        return line[:-1]
    return line


def iter_sse_events(path: Path, *, last_event_id: str | None = None, poll_interval: float = 0.1) -> Iterator[str]:
    offset = 0
    pending = ''
    next_event_id = _parse_last_event_id(last_event_id)
    line_id = 0

    while True:
        if path.exists():
            file_size = path.stat().st_size
            if file_size < offset:
                offset = 0
                pending = ''
                line_id = 0

            with path.open('r', encoding='utf-8') as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()

            if chunk:
                pending += chunk
                lines = pending.splitlines(keepends=True)
                if lines and not lines[-1].endswith(('\n', '\r')):
                    pending = lines.pop()
                else:
                    pending = ''

                for line in lines:
                    event_id = line_id
                    line_id += 1
                    if event_id < next_event_id:
                        continue

                    yield f'id: {event_id}\ndata: {_strip_line_ending(line)}\n\n'
                continue

        sleep(poll_interval)
