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


def iter_sse_events(path: Path, *, poll_interval: float = 0.1) -> Iterator[str]:
    offset = 0
    pending = ''

    while True:
        if path.exists():
            file_size = path.stat().st_size
            if file_size < offset:
                offset = 0
                pending = ''

            with path.open('r', encoding='utf-8') as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()

            if chunk:
                pending += chunk
                lines = pending.splitlines(keepends=True)
                if lines and not lines[-1].endswith('\n'):
                    pending = lines.pop()
                else:
                    pending = ''

                for line in lines:
                    yield f'data: {line.rstrip()}\n\n'
                continue

        sleep(poll_interval)
