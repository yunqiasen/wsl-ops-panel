"""Regression seams for terminal text, bounded task history and system scans."""

from datetime import UTC, datetime
from pathlib import Path
import subprocess
from threading import Lock
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.api.tasks import _read_text_excerpt, tasks_page
from app.models.tasks import TaskRecord
from app.tasks.store import InMemoryTaskStore, SQLiteTaskStore
from app.terminals.debug_terminal import _DebugTerminalRuntime


def task_record(number, status="succeeded"):
    return TaskRecord(
        id=str(number),
        object_id="fixture",
        action="check",
        status=status,
        stdout_log_path="",
        stderr_log_path="",
        created_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 5, 4096])
@pytest.mark.parametrize("end", ["eof", "eio"])
def test_pty_keeps_utf8_text_across_every_read_size(
    tmp_path, monkeypatch, chunk_size, end
):
    runtime = _DebugTerminalRuntime.__new__(_DebugTerminalRuntime)
    runtime._master_fd = 99
    runtime._io_lock = Lock()
    runtime.session = SimpleNamespace(output_log_path=str(tmp_path / "pty.log"))
    chunks = []
    runtime._broadcast = chunks.append
    text = "中文🙂\r\n$ echo 测试\x1b[0m"
    raw = text.encode()
    reads = iter(raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size))

    def read(*_):
        chunk = next(reads, None)
        if chunk is not None:
            return chunk
        if end == "eio":
            raise OSError(5, "PTY closed")
        return b""

    monkeypatch.setattr("app.terminals.debug_terminal.os.read", read)
    runtime._read_loop()
    assert "".join(chunks) == text
    assert (tmp_path / "pty.log").read_bytes().decode() == text
    assert "" not in chunks


def test_pty_flushes_incomplete_final_character_once(tmp_path, monkeypatch):
    runtime = _DebugTerminalRuntime.__new__(_DebugTerminalRuntime)
    runtime._master_fd = 99
    runtime._io_lock = Lock()
    runtime.session = SimpleNamespace(output_log_path=str(tmp_path / "pty.log"))
    chunks = []
    runtime._broadcast = chunks.append
    reads = iter([b"prefix\xe4", b"\xb8", b""])
    monkeypatch.setattr("app.terminals.debug_terminal.os.read", lambda *_: next(reads))
    runtime._read_loop()
    assert "".join(chunks) == "prefix�"
    assert (tmp_path / "pty.log").read_text() == "prefix�"


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_recent_tasks_bound_results_and_keep_global_counts(tmp_path, kind):
    store = (
        InMemoryTaskStore()
        if kind == "memory"
        else SQLiteTaskStore(tmp_path / "tasks.sqlite")
    )
    for i in range(125):
        store.insert(task_record(i, "failed" if i < 5 else "succeeded"))
    assert [t.id for t in store.list_recent(limit=3)] == ["124", "123", "122"]
    assert store.count_statuses() == dict(
        queued=0, running=0, succeeded=120, failed=5, interrupted=0, total=125
    )
    assert store.list_recent(limit=0) == []
    with pytest.raises(ValueError):
        store.list_recent(limit=-1)


def test_task_page_never_loads_all_history(monkeypatch):
    store = InMemoryTaskStore()
    for i in range(125):
        store.insert(task_record(i))

    def all_history():
        pytest.fail("task page loaded all historical TaskRecords")

    monkeypatch.setattr(store, "list_all", all_history)
    monkeypatch.setattr("app.api.tasks.page_login_redirect", lambda _: None)
    monkeypatch.setattr("app.api.tasks.build_page_context", lambda _, **kw: kw)
    monkeypatch.setattr("app.api.tasks._build_task_item", lambda task: {"id": task.id})
    monkeypatch.setattr(
        "app.api.tasks.TEMPLATES",
        SimpleNamespace(TemplateResponse=lambda _, __, context: context),
    )
    request = Request(
        {
            "type": "http",
            "headers": [],
            "app": SimpleNamespace(state=SimpleNamespace(task_store=store)),
        }
    )
    context = tasks_page(request)
    assert context["shown_task_count"] == 120
    assert context["task_stats"]["total"] == 125
    assert context["task_items"][0]["id"] == "124"
    assert len(context["tasks"]) == 120


def test_task_log_read_has_io_budget_and_keeps_utf8_tail(tmp_path, monkeypatch):
    path = tmp_path / "huge.log"
    text = "多字节🙂log\r\n" * 30000
    path.write_text(text)
    original_open = Path.open
    reads = []

    class FileProbe:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            self.fh.__enter__()
            return self

        def __exit__(self, *args):
            return self.fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.fh, name)

        def read(self, size=-1):
            reads.append(size)
            assert 0 <= size <= 4 * 6000 + 3, "unbounded file read"
            return self.fh.read(size)

    def opened(candidate, *args, **kwargs):
        fh = original_open(candidate, *args, **kwargs)
        return FileProbe(fh) if candidate == path else fh

    monkeypatch.setattr(Path, "open", opened)
    assert _read_text_excerpt(path, max_chars=6000) == text[-6000:]
    assert sum(reads) <= 4 * 6000 + 3


def test_task_log_handles_missing_empty_and_zero_limit(tmp_path):
    path = tmp_path / "absent.log"
    assert _read_text_excerpt(path) == ""
    path.write_text("中文")
    assert _read_text_excerpt(path, max_chars=0) == ""
    assert _read_text_excerpt(path, max_chars=1) == "文"


def test_system_scan_timeout_keeps_other_tools(monkeypatch):
    from app.scanners import system_scanner

    monkeypatch.setattr(
        system_scanner,
        "SYSTEM_COMMANDS",
        {"slow": ["slow"], "node": ["node", "--version"]},
    )

    def slow():
        raise subprocess.TimeoutExpired(["slow"], 10)

    assets = system_scanner.scan_system_infrastructure(
        runners={
            "slow": slow,
            "node": lambda: subprocess.CompletedProcess(
                [], 0, stdout="v22.0.0", stderr=""
            ),
        }
    )
    assert [(a.name, a.status) for a in assets] == [
        ("slow", "error"),
        ("node", "available"),
    ]
    assert assets[0].supports_actions == []


def test_system_commands_always_have_timeout(monkeypatch):
    from app.scanners.system_scanner import _run_command

    options = {}

    def run(command, **kwargs):
        options.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="v1.0", stderr="")

    monkeypatch.setattr("app.scanners.system_scanner.subprocess.run", run)
    _run_command(["fixture", "--version"])
    assert 0 < options.get("timeout", 0) <= 15


def test_system_operation_uses_stable_key_not_display_name():
    from app.api.assets import _build_adapter
    from app.models.assets import AssetSnapshot

    asset = AssetSnapshot(
        object_id="system__apt_packages",
        category="system",
        status="available",
        name="系统包检查（显示名）",
        metadata={"system_key": "apt_packages"},
    )
    plan = _build_adapter(None, asset.object_id, asset).plan_action("update_latest")
    assert plan.commands == [["sudo", "apt", "update"], ["apt", "list", "--upgradable"]]
