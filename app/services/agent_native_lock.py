"""Serialize panel-owned native writes for one client, including Router takeover."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Stable sidecar inode: serializes threads and independent panel processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


@contextmanager
def native_client_lock(data_root: Path, client_id: str) -> Iterator[None]:
    with exclusive_file_lock(data_root / f".projection-{client_id}.lock"):
        yield
