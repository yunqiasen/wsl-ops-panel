from collections.abc import Iterator
from pathlib import Path


class SystemTerminalSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def write(self, chunk: str) -> None:
        with self.path.open('a', encoding='utf-8') as fh:
            fh.write(chunk)


def iter_sse_events(path: Path) -> Iterator[str]:
    if not path.exists():
        return

    for line in path.read_text(encoding='utf-8').splitlines():
        yield f'data: {line}\n\n'
