"""Persistent regression tests for config_sync append_line flow (F05/F06).

F05: read failure must preserve original file content.
F06: write failure must return non-zero exit code.

Additional coverage: idempotency, trailing newline, dash-prefixed lines,
special characters, symlink protection, permission preservation, backup
uniqueness, directory refusal, editable gate, Windows static verification.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest

from app.services.config_sync import config_apply_command


# ── helpers ──────────────────────────────────────────────────────────────


def _run_unix_append(
    home: Path,
    module_id: str = "zshrc",
    content: str = "export REVIEW_NEW=1",
) -> subprocess.CompletedProcess[str]:
    command = config_apply_command(module_id, "append_line", content)
    assert command is not None, "command should be generated"
    return subprocess.run(
        ["bash", "-c", command],
        env={**os.environ, "HOME": str(home), "TMPDIR": str(home.parent)},
        capture_output=True,
        text=True,
        timeout=10,
    )


# ── F05: read failure preserves original file ─────────────────────────────


def test_read_failure_preserves_original_file(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission fixture requires non-root")
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".zshrc"
    target.write_text("export REVIEW_EXISTING=1\n")
    target.chmod(0)
    try:
        result = _run_unix_append(home)
    finally:
        target.chmod(0o600)
    assert target.read_text() == "export REVIEW_EXISTING=1\n", (
        result.returncode,
        result.stderr,
    )
    assert result.returncode != 0, (result.returncode, result.stderr)


# ── F06: write failure returns failure ────────────────────────────────────


def test_write_failure_returns_failure(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission fixture requires non-root")
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".zshrc"
    target.write_text("export REVIEW_EXISTING=1\n")
    home.chmod(0o500)
    try:
        result = _run_unix_append(home)
    finally:
        home.chmod(0o700)
    assert result.returncode != 0, (result.returncode, result.stderr)


# ── idempotent append ─────────────────────────────────────────────────────


def test_idempotent_append_already_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export REVIEW_NEW=1\n")
    result = _run_unix_append(home)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert (home / ".zshrc").read_text() == "export REVIEW_NEW=1\n"


# ── no trailing newline in original ──────────────────────────────────────


def test_append_to_file_without_trailing_newline(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export EXISTING=1")
    result = _run_unix_append(home)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert (home / ".zshrc").read_text() == ("export EXISTING=1\nexport REVIEW_NEW=1\n")


# ── line starting with dash ──────────────────────────────────────────────


def test_append_line_starting_with_dash(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("existing\n")
    result = _run_unix_append(home, content="--my-flag")
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert (home / ".zshrc").read_text() == "existing\n--my-flag\n"


# ── special characters ──────────────────────────────────────────────────


def test_append_line_with_dollar_and_quotes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("existing\n")
    special = 'export FOO="bar $baz" # comment'
    result = _run_unix_append(home, content=special)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert (home / ".zshrc").read_text() == "existing\n" + special + "\n"


def test_append_line_with_single_quote(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("existing\n")
    line = "export FOO='bar'"
    result = _run_unix_append(home, content=line)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert (home / ".zshrc").read_text() == "existing\n" + line + "\n"


# ── symlink protection ───────────────────────────────────────────────────


def test_symlink_not_broken(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    real = home / "real_zshrc"
    real.write_text("export REAL=1\n")
    link = home / ".zshrc"
    link.symlink_to(real)
    result = _run_unix_append(home)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert link.is_symlink(), "symlink should not be broken"
    assert "export REVIEW_NEW=1" in real.read_text()


# ── permission preservation ───────────────────────────────────────────────


def test_permissions_preserved(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".zshrc"
    target.write_text("existing\n")
    target.chmod(0o644)
    result = _run_unix_append(home)
    assert result.returncode == 0, (result.returncode, result.stderr)
    mode = target.stat().st_mode & 0o777
    assert mode == 0o644, oct(mode)


# ── non-editable / directory modules ─────────────────────────────────────


def test_non_editable_directory_module_returns_none() -> None:
    assert config_apply_command("apt_sources_d", "append_line", "test") is None


def test_powershell_profile_returns_none() -> None:
    assert config_apply_command("powershell_profile", "append_line", "test") is None


# ── backup created ───────────────────────────────────────────────────────


def test_backup_created(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export ORIGINAL=1\n")
    result = _run_unix_append(home)
    assert result.returncode == 0, (result.returncode, result.stderr)
    backups = list(home.glob(".zshrc.wsl-ops-bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "export ORIGINAL=1\n"


# ── same-second backup not overwritten ───────────────────────────────────


def test_same_second_backup_not_overwritten(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("export ORIGINAL=1\n")
    r1 = _run_unix_append(home, content="export LINE1=1")
    r2 = _run_unix_append(home, content="export LINE2=1")
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    backups = list(home.glob(".zshrc.wsl-ops-bak-*"))
    assert len(backups) >= 2, f"expected >=2 backups, got {len(backups)}"


# ── no temp file left behind ──────────────────────────────────────────────


def test_no_temp_file_left(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("existing\n")
    _run_unix_append(home)
    temp_files = [f for f in home.iterdir() if f.name.startswith(".wsl-ops-tmp")]
    assert len(temp_files) == 0


# ── directory target refused ─────────────────────────────────────────────


def test_directory_target_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").mkdir()
    result = _run_unix_append(home)
    assert result.returncode != 0, (result.returncode, result.stderr)


# ── read-back verification ───────────────────────────────────────────────


def test_read_back_verification_output(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("existing\n")
    result = _run_unix_append(home)
    assert result.returncode == 0
    assert "applied" in result.stdout


# ── Windows static verification ───────────────────────────────────────────


def _decode_windows_command(module_id: str, content: str = "export FOO=1") -> str:
    cmd = config_apply_command(module_id, "append_line", content, windows=True)
    assert cmd is not None, f"command should be generated for {module_id}"
    prefix = "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
    assert cmd.startswith(prefix)
    encoded = cmd[len(prefix) :]
    return base64.b64decode(encoded).decode("utf-16le")


def test_windows_gitconfig_uses_real_userprofile() -> None:
    script = _decode_windows_command("gitconfig")
    assert "%USERPROFILE%" not in script, (
        "should not use cmd.exe-style %USERPROFILE% in PowerShell"
    )
    assert "$env:USERPROFILE" in script, "should use PowerShell $env:USERPROFILE"


def test_windows_ssh_config_uses_real_userprofile() -> None:
    script = _decode_windows_command("ssh_config")
    assert "%USERPROFILE%" not in script
    assert "$env:USERPROFILE" in script


def test_windows_has_error_action_stop() -> None:
    script = _decode_windows_command("gitconfig")
    assert "$ErrorActionPreference = 'Stop'" in script


def test_windows_has_try_catch() -> None:
    script = _decode_windows_command("gitconfig")
    assert "try {" in script
    assert "} catch {" in script


def test_windows_has_read_back_verify() -> None:
    script = _decode_windows_command("gitconfig")
    assert "verification" in script.lower()


def test_windows_backup_has_uniqueness() -> None:
    script = _decode_windows_command("gitconfig")
    assert "while" in script.lower() and "bak" in script.lower()


def test_windows_directory_check() -> None:
    script = _decode_windows_command("gitconfig")
    assert "PathType Container" in script or "Container" in script


@pytest.mark.parametrize("content", ["a\nb", "a\rb", "a\x00b"])
def test_append_rejects_multiple_lines_and_nul(content):
    assert config_apply_command("zshrc", "append_line", content) is None


def test_new_ssh_directory_is_created_with_private_permissions(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = _run_unix_append(home, module_id="ssh_config", content="Host fixture")
    assert result.returncode == 0, result.stderr
    assert (home / ".ssh/config").read_text() == "Host fixture\n"
    assert (home / ".ssh/config").stat().st_mode & 0o777 == 0o600
    assert (home / ".ssh").stat().st_mode & 0o777 == 0o700


def test_relative_symlink_is_preserved(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "actual").write_text("original\n")
    (home / ".zshrc").symlink_to("actual")
    result = _run_unix_append(home)
    assert result.returncode == 0, result.stderr
    assert (home / ".zshrc").is_symlink()
    assert (home / "actual").read_text() == "original\nexport REVIEW_NEW=1\n"


@pytest.mark.parametrize("tool", ["cp", "mv", "cmp"])
def test_file_tool_failure_never_reports_applied(tmp_path, tool):
    home = tmp_path / "home"
    home.mkdir()
    target = home / ".zshrc"
    target.write_text("original\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / tool
    fake.write_text("#!/bin/sh\nexit 7\n")
    fake.chmod(0o700)
    command = config_apply_command("zshrc", "append_line", "new line")
    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "applied" not in result.stdout
    assert target.read_text() == "original\n"
    assert not list(home.glob("*.wsl-ops-lock"))


def test_windows_append_holds_exclusive_lock_from_read_through_verification():
    script = _decode_windows_command('gitconfig')
    acquire = script.index('[IO.FileMode]::CreateNew')
    read = script.index('[IO.File]::ReadAllBytes($p)')
    verify = script.index('configuration verification failed')
    release = script.index('$lockStream.Dispose()')
    assert acquire < read < verify < release
    assert '[IO.FileShare]::None' in script
    assert 'if ($lockStream)' in script
