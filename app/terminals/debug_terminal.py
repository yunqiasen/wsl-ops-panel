import asyncio
import codecs
import fcntl
import json
import os
import shutil
import signal
import subprocess
import struct
import termios
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Thread
from uuid import uuid4

from app.models.terminals import DebugTerminalSession

DEFAULT_DEBUG_TERMINAL_CWD = Path('/home/div/1_Project_dir/AI')
DEFAULT_DEBUG_TERMINAL_SHELL = shutil.which('zsh') or '/bin/bash'
SYSTEM_TERMINAL_ID = 'system'


@dataclass(slots=True)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[str | None]


class _DebugTerminalRuntime:
    def __init__(self, session: DebugTerminalSession, *, argv: list[str]) -> None:
        self.session = session
        self.argv = argv
        self._lock = Lock()
        self._io_lock = Lock()
        self._subscriber_lock = Lock()
        self._subscribers: list[_Subscriber] = []
        self._finalized = False
        self._master_fd: int | None = None

        master_fd, slave_fd = os.openpty()
        env = os.environ.copy()
        env.setdefault('TERM', 'xterm-256color')
        env.setdefault('LANG', 'C.UTF-8')
        env.setdefault('LC_ALL', env.get('LANG', 'C.UTF-8'))
        env.setdefault('LC_CTYPE', env.get('LANG', 'C.UTF-8'))
        if not env.get('COLORTERM'):
            env['COLORTERM'] = 'truecolor'

        try:
            process = subprocess.Popen(
                argv,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=session.cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)

        self.process = process
        self.session.pid = process.pid
        self._master_fd = master_fd
        self._write_metadata()

        self._reader_thread = Thread(target=self._read_loop, name=f'debug-terminal-reader-{session.id}', daemon=True)
        self._waiter_thread = Thread(target=self._wait_loop, name=f'debug-terminal-waiter-{session.id}', daemon=True)
        self._reader_thread.start()
        self._waiter_thread.start()

    def snapshot(self) -> DebugTerminalSession:
        with self._lock:
            return self.session.model_copy(deep=True)

    def is_closed(self) -> bool:
        return self.process.poll() is not None

    def read_output_log(self, *, max_bytes: int = 200_000) -> str:
        path = Path(self.session.output_log_path)
        size = path.stat().st_size if path.exists() else 0
        with path.open('rb') as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode('utf-8', errors='replace')

    def attach(self, loop: asyncio.AbstractEventLoop) -> tuple[str, asyncio.Queue[str | None]]:
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        with self._io_lock:
            backlog = self.read_output_log()
            with self._subscriber_lock:
                self._subscribers.append(_Subscriber(loop=loop, queue=queue))
            if self.is_closed():
                queue.put_nowait(None)
        return backlog, queue

    def unsubscribe(self, queue: asyncio.Queue[str | None]) -> None:
        with self._subscriber_lock:
            self._subscribers = [item for item in self._subscribers if item.queue is not queue]

    def write_input(self, chunk: str) -> None:
        master_fd = self._master_fd
        if master_fd is None:
            raise RuntimeError('session already closed')

        os.write(master_fd, chunk.encode('utf-8'))
        with Path(self.session.input_log_path).open('a', encoding='utf-8') as fh:
            fh.write(chunk)

    def resize(self, *, cols: int, rows: int) -> None:
        master_fd = self._master_fd
        if master_fd is None:
            raise RuntimeError('session already closed')
        if cols < 2 or rows < 1:
            return
        size = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGWINCH)

    def close(self) -> None:
        if self.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=1)

        self._waiter_thread.join(timeout=1)
        self._reader_thread.join(timeout=1)
        if not self._finalized:
            self._finalize(self.process.poll())

    def _read_loop(self) -> None:
        # A PTY read can split any UTF-8 code point; retain bytes per session.
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while True:
            master_fd = self._master_fd
            if master_fd is None:
                break
            try:
                data = os.read(master_fd, 4096)
            except InterruptedError:
                continue
            except OSError:
                break
            if not data:
                break
            self._append_output(decoder.decode(data))
        self._append_output(decoder.decode(b'', final=True))

    def _append_output(self, text: str) -> None:
        if not text:
            return
        with self._io_lock:
            with Path(self.session.output_log_path).open('a', encoding='utf-8') as fh:
                fh.write(text)
            self._broadcast(text)

    def _wait_loop(self) -> None:
        exit_code = self.process.wait()
        self._reader_thread.join(timeout=1)
        self._finalize(exit_code)

    def _finalize(self, exit_code: int | None) -> None:
        with self._io_lock:
            with self._lock:
                if self._finalized:
                    return
                self.session.status = 'closed'
                self.session.exit_code = exit_code
                self.session.closed_at = datetime.now(UTC)
                self._finalized = True
                self._write_metadata()

            self._broadcast(None)
            self._close_master_fd()

    def _close_master_fd(self) -> None:
        with self._lock:
            master_fd = self._master_fd
            self._master_fd = None
        if master_fd is not None:
            with suppress(OSError):
                os.close(master_fd)

    def _broadcast(self, chunk: str | None) -> None:
        stale: list[_Subscriber] = []
        with self._subscriber_lock:
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, chunk)
            except RuntimeError:
                stale.append(subscriber)

        if stale:
            with self._subscriber_lock:
                self._subscribers = [item for item in self._subscribers if item not in stale]

    def _write_metadata(self) -> None:
        payload = self.session.model_dump(mode='json')
        Path(self.session.metadata_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


class DebugTerminalManager:
    def __init__(
        self,
        *,
        base_dir: Path | str = Path('data/terminals'),
        default_cwd: Path | str = DEFAULT_DEBUG_TERMINAL_CWD,
        default_shell: str = DEFAULT_DEBUG_TERMINAL_SHELL,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.default_cwd = Path(default_cwd)
        self.default_shell = default_shell
        self._lock = Lock()
        self._sessions: dict[str, _DebugTerminalRuntime] = {}

    def ensure_system_session(self) -> DebugTerminalSession:
        return DebugTerminalSession(
            id=SYSTEM_TERMINAL_ID,
            title='系统日志',
            shell='readonly-log',
            cwd=str(self.default_cwd),
            status='running',
            created_at=datetime.now(UTC),
            output_log_path=str(self.base_dir / 'system.log'),
            input_log_path=str(self.base_dir / 'system.input.log'),
            metadata_path=str(self.base_dir / 'system.json'),
        )

    def create_session(self, *, shell: str | None = None, cwd: str | None = None, title: str | None = None) -> DebugTerminalSession:
        return self._ensure_session(shell=shell, cwd=cwd, title=title)

    def _ensure_session(
        self,
        *,
        session_id: str | None = None,
        shell: str | None = None,
        cwd: str | None = None,
        title: str | None = None,
    ) -> DebugTerminalSession:
        if session_id is not None:
            with self._lock:
                existing = self._sessions.get(session_id)
            if existing is not None and not existing.is_closed():
                return existing.snapshot()

        resolved_cwd = self._resolve_cwd(cwd)
        resolved_shell, argv = self._resolve_shell(shell)
        resolved_session_id = session_id or uuid4().hex
        created_at = datetime.now(UTC)
        session = DebugTerminalSession(
            id=resolved_session_id,
            title=title.strip()[:64] if title and title.strip() else None,
            shell=resolved_shell,
            cwd=str(resolved_cwd),
            status='running',
            created_at=created_at,
            output_log_path=str(self.base_dir / f'{resolved_session_id}.log'),
            input_log_path=str(self.base_dir / f'{resolved_session_id}.input.log'),
            metadata_path=str(self.base_dir / f'{resolved_session_id}.json'),
        )
        Path(session.output_log_path).touch()
        Path(session.input_log_path).touch()

        runtime = _DebugTerminalRuntime(session, argv=argv)
        with self._lock:
            self._sessions[resolved_session_id] = runtime
        return runtime.snapshot()

    def list_sessions(self) -> list[DebugTerminalSession]:
        with self._lock:
            runtimes = list(self._sessions.values())
        return [
            self.ensure_system_session(),
            *sorted((runtime.snapshot() for runtime in runtimes), key=lambda session: session.created_at),
        ]

    def get_runtime(self, session_id: str) -> _DebugTerminalRuntime | None:
        if session_id == SYSTEM_TERMINAL_ID:
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def rename_session(self, session_id: str, title: str) -> DebugTerminalSession:
        if session_id == SYSTEM_TERMINAL_ID:
            raise ValueError('system terminal cannot be renamed')
        resolved_title = title.strip()
        if not resolved_title:
            raise ValueError('title is required')
        with self._lock:
            runtime = self._sessions.get(session_id)
        if runtime is None:
            raise KeyError(f'unknown terminal session: {session_id}')
        with runtime._lock:
            runtime.session.title = resolved_title[:64]
            runtime._write_metadata()
        return runtime.snapshot()

    def close_session(self, session_id: str, *, force: bool = False) -> bool:
        if session_id == SYSTEM_TERMINAL_ID and not force:
            raise ValueError('system terminal cannot be closed')
        with self._lock:
            runtime = self._sessions.pop(session_id, None)
        if runtime is None:
            return False
        runtime.close()
        return True

    def close_all(self) -> None:
        with self._lock:
            session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            self.close_session(session_id, force=True)

    def _resolve_cwd(self, cwd: str | None) -> Path:
        candidate = Path(cwd) if cwd is not None else self.default_cwd
        resolved = candidate.expanduser().resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f'invalid cwd: {candidate}')
        return resolved

    def _resolve_shell(self, shell: str | None) -> tuple[str, list[str]]:
        candidate = shell or self.default_shell
        resolved = shutil.which(candidate) if not Path(candidate).is_absolute() else candidate
        if resolved is None:
            raise ValueError(f'invalid shell: {candidate}')
        resolved_path = Path(resolved)
        if not resolved_path.exists() or not os.access(resolved_path, os.X_OK):
            raise ValueError(f'invalid shell: {candidate}')

        argv = [str(resolved_path)]
        if resolved_path.name == 'bash':
            argv = [str(resolved_path), '--noprofile', '--norc', '-i']
        elif resolved_path.name == 'zsh':
            argv = [str(resolved_path), '-i']
        return str(resolved_path), argv
