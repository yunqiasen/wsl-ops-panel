from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConfigModuleDefinition:
    id: str
    label: str
    path: str
    group: str
    applies_to: str
    compatible_os: tuple[str, ...]
    requires_sudo: bool = False
    editable: bool = True
    note: str = ''


CONFIG_MODULE_DEFINITIONS = [
    ConfigModuleDefinition('zshrc', 'Zsh 启动配置', '~/.zshrc', 'Shell', 'Linux / WSL / macOS', ('linux', 'wsl', 'macos'), note='alias、PATH、代理、nvm、bun 等常见入口。'),
    ConfigModuleDefinition('bashrc', 'Bash 启动配置', '~/.bashrc', 'Shell', 'Linux / WSL / macOS', ('linux', 'wsl', 'macos'), note='bash alias、PATH、代理和 CLI 初始化。'),
    ConfigModuleDefinition('profile', 'Profile 基础配置', '~/.profile', 'Shell', 'Linux / WSL / macOS', ('linux', 'wsl', 'macos'), note='登录 shell 基础环境变量。'),
    ConfigModuleDefinition('powershell_profile', 'PowerShell Profile', '$PROFILE', 'Shell', 'Windows', ('windows',), editable=False, note='Windows PowerShell 启动配置。Windows 不写 ~/.zshrc，应该改这个。'),
    ConfigModuleDefinition('gitconfig', 'Git 全局配置', '~/.gitconfig', 'Git', 'Linux / WSL / macOS / Windows', ('linux', 'wsl', 'macos', 'windows'), note='用户名、代理、URL rewrite。'),
    ConfigModuleDefinition('ssh_config', 'SSH 客户端配置', '~/.ssh/config', 'SSH', 'Linux / WSL / macOS / Windows', ('linux', 'wsl', 'macos', 'windows'), note='只同步 config，不同步私钥和 known_hosts。'),
    ConfigModuleDefinition('npmrc', 'npm 配置', '~/.npmrc', 'Node', 'Linux / WSL / macOS / Windows', ('linux', 'wsl', 'macos', 'windows'), note='registry、proxy、prefix。'),
    ConfigModuleDefinition('wsl_conf', 'WSL 配置', '/etc/wsl.conf', 'WSL', 'WSL', ('wsl',), requires_sudo=True, note='WSL 启动、网络、systemd 等配置。'),
    ConfigModuleDefinition('environment', '系统环境变量', '/etc/environment', 'System', 'Linux / WSL', ('linux', 'wsl'), requires_sudo=True, note='系统级 PATH / proxy。'),
    ConfigModuleDefinition('apt_sources', 'APT 主源', '/etc/apt/sources.list', 'APT', 'Linux / WSL', ('linux', 'wsl'), requires_sudo=True, note='Ubuntu / Debian 主软件源。'),
    ConfigModuleDefinition('apt_sources_d', 'APT 源目录', '/etc/apt/sources.list.d', 'APT', 'Linux / WSL', ('linux', 'wsl'), requires_sudo=True, editable=False, note='Docker、Tailscale、NodeSource 等源文件目录。'),
    ConfigModuleDefinition('docker_daemon', 'Docker daemon 配置', '/etc/docker/daemon.json', 'Docker', 'Linux / WSL', ('linux', 'wsl'), requires_sudo=True, note='Docker 镜像源、代理、日志等。'),
]


def build_config_modules() -> list[dict[str, Any]]:
    return [_module_payload(definition) for definition in CONFIG_MODULE_DEFINITIONS]


def get_config_definition(module_id: str) -> ConfigModuleDefinition | None:
    return next((item for item in CONFIG_MODULE_DEFINITIONS if item.id == module_id), None)


def is_config_module_compatible(module_id: str, os_kind: str) -> bool:
    definition = get_config_definition(module_id)
    if definition is None:
        return False
    return os_kind in definition.compatible_os or (os_kind == 'unknown' and 'windows' not in definition.compatible_os)


def remote_config_scan_command(module_id: str, *, windows: bool = False) -> str | None:
    definition = get_config_definition(module_id)
    if definition is None:
        return None
    if windows:
        if 'windows' not in definition.compatible_os:
            return f'echo ## {definition.label} & echo not-applicable-on-windows & exit /b 0'
        if definition.id == 'powershell_profile':
            return 'powershell -NoProfile -Command "Write-Output \"## PowerShell Profile\"; Write-Output $PROFILE; if(Test-Path $PROFILE){Get-Item $PROFILE | Format-List FullName,Length,LastWriteTime}else{Write-Output missing}"'
        win_path = _windows_path(definition.path)
        return f'echo ## {definition.label} & if exist "{win_path}" dir "{win_path}" & if not exist "{win_path}" echo missing & exit /b 0'
    if 'windows' in definition.compatible_os and len(definition.compatible_os) == 1:
        return f'echo "## {definition.label}"; echo not-applicable-on-unix; true'
    shell_path = definition.path
    return (
        f'echo "## {definition.label}"; '
        f'p={_sq(shell_path)}; eval "x=$p"; '
        'if [ -e "$x" ]; then '
        'ls -ld "$x"; '
        'if [ -f "$x" ]; then sha256sum "$x" 2>/dev/null || shasum -a 256 "$x" 2>/dev/null || true; fi; '
        'else echo missing; fi; true'
    )


def config_apply_command(module_id: str, operation: str, content: str, *, windows: bool = False) -> str | None:
    definition = get_config_definition(module_id)
    if definition is None or not definition.editable or operation != 'append_line':
        return None
    line = content.strip('\r\n')
    if not line or '\n' in line or '\r' in line or '\x00' in line:
        return None
    if windows:
        if 'windows' not in definition.compatible_os:
            return None
        return _windows_append_line_command(definition, line)
    if 'windows' in definition.compatible_os and len(definition.compatible_os) == 1:
        return None
    return _unix_append_line_command(definition.path, line)


def get_config_module(module_id: str) -> dict[str, Any] | None:
    definition = get_config_definition(module_id)
    if definition is None:
        return None
    return _module_payload(definition)


def read_config_module_text(module_id: str) -> tuple[str, str | None]:
    definition = get_config_definition(module_id)
    if definition is None:
        return '', '配置模块不存在'
    path = _expand_path(definition.path)
    if not path.exists():
        return '', '原文件不存在，会先保存为草稿'
    if not path.is_file():
        return '', '这是目录模块，当前只支持查看状态，不直接编辑目录'
    try:
        if path.stat().st_size > 512_000:
            return '', '文件太大，面板暂不加载正文'
        return path.read_text(encoding='utf-8', errors='replace'), None
    except Exception as exc:
        return '', f'读取失败：{exc}'


def config_draft_path(config_root: Path | str, module_id: str) -> Path:
    safe_id = ''.join(ch for ch in module_id if ch.isalnum() or ch in {'-', '_'}) or 'config'
    root = Path(config_root)
    base = root.parent if root.name == 'config' else root
    return base / 'data' / 'config_sync_drafts' / f'{safe_id}.txt'


def read_config_draft(config_root: Path | str, module_id: str) -> str | None:
    path = config_draft_path(config_root, module_id)
    if not path.exists():
        return None
    return path.read_text(encoding='utf-8', errors='replace')


def save_config_draft(config_root: Path | str, module_id: str, text: str) -> Path:
    path = config_draft_path(config_root, module_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def _module_payload(definition: ConfigModuleDefinition) -> dict[str, Any]:
    path = _expand_path(definition.path)
    exists = path.exists()
    size = 0
    updated_at = '不存在'
    summary = definition.note
    fingerprint = ''
    if exists:
        try:
            stat = path.stat()
            size = stat.st_size
            updated_at = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M')
            if path.is_file():
                fingerprint = _file_fingerprint(path)
                summary = _content_summary(path, definition.note)
            elif path.is_dir():
                names = sorted(child.name for child in path.iterdir())[:6]
                summary = f'{definition.note} 当前目录项：{", ".join(names) if names else "空目录"}'
        except Exception as exc:
            summary = f'{definition.note} 扫描失败：{exc}'
    return {
        'id': definition.id,
        'label': definition.label,
        'path': definition.path,
        'resolved_path': str(path),
        'group': definition.group,
        'applies_to': definition.applies_to,
        'compatible_os': ','.join(definition.compatible_os),
        'requires_sudo': definition.requires_sudo,
        'editable': definition.editable and exists and path.is_file(),
        'append_supported': definition.editable,
        'exists': exists,
        'status': '存在' if exists else '未创建',
        'updated_at': updated_at,
        'size_label': _size_label(size),
        'summary': summary,
        'fingerprint': fingerprint,
    }


def _expand_path(value: str) -> Path:
    if value == '$PROFILE':
        return Path.home() / 'Documents' / 'PowerShell' / 'Microsoft.PowerShell_profile.ps1'
    if value.startswith('~/'):
        return Path.home() / value[2:]
    return Path(value)


def _content_summary(path: Path, fallback: str) -> str:
    try:
        if path.stat().st_size > 1_000_000:
            return f'{fallback} 文件较大，仅展示状态。'
        text = path.read_text(encoding='utf-8', errors='ignore').lower()
    except Exception:
        return fallback
    hits = [key for key in ['proxy', 'path', 'alias', 'export', 'registry', 'index-url', 'nvm', 'pnpm', 'bun', 'conda', 'docker'] if key in text]
    return f'{fallback} 关键词：{", ".join(hits) if hits else "未检测到明显关键词"}。'


def _file_fingerprint(path: Path) -> str:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ''
    return digest[:12]


def _size_label(size: int) -> str:
    if size >= 1024 * 1024:
        return f'{size / 1024 / 1024:.1f} MB'
    if size >= 1024:
        return f'{size / 1024:.1f} KB'
    return f'{size} B'


def _windows_path(path: str) -> str:
    if path == '$PROFILE':
        return '%USERPROFILE%\\Documents\\PowerShell\\Microsoft.PowerShell_profile.ps1'
    if path == '~/.gitconfig':
        return '%USERPROFILE%\\.gitconfig'
    if path == '~/.ssh/config':
        return '%USERPROFILE%\\.ssh\\config'
    if path == '~/.npmrc':
        return '%USERPROFILE%\\.npmrc'
    if path.startswith('~/'):
        return '%USERPROFILE%\\' + path[2:].replace('/', '\\')
    return path


def _unix_append_line_command(path: str, line: str) -> str:
    # Only POSIX shell and standard file tools are required on the target.
    script = r'''set -eu
umask 077
target=__PATH__
line=__LINE__
case "$target" in '~/'*) target="$HOME/${target#\~/}" ;; esac
# Follow final symlinks without GNU-only readlink -f, retaining the link itself.
links=0
while [ -L "$target" ]; do
    links=$((links + 1))
    [ "$links" -le 40 ] || { echo 'configuration symlink loop' >&2; exit 1; }
    link=$(readlink "$target")
    case "$link" in /*) target="$link" ;; *) target="$(dirname "$target")/$link" ;; esac
done
parent=$(dirname "$target")
mkdir -p "$parent"
parent=$(CDPATH='' cd -P "$parent" && pwd)
target="$parent/$(basename "$target")"
lock="$target.wsl-ops-lock"
mkdir "$lock" || { echo 'configuration is busy or directory is not writable' >&2; exit 1; }
trap 'rm -f "$lock/next" "$lock/expected"; rmdir "$lock"' 0
trap 'exit 1' 1 2 15
tmp="$lock/next"
existed=0
if [ -e "$target" ]; then
    [ -f "$target" ] && [ -r "$target" ] && [ -w "$target" ] || {
        echo 'configuration must be a readable writable regular file' >&2; exit 1;
    }
    existed=1
    backup=$(mktemp "$target.wsl-ops-bak-XXXXXX")
    cp -p "$target" "$backup"
    cp -p "$target" "$tmp"
    chmod 600 "$backup"
else
    : > "$tmp"
fi
if grep -qxF -e "$line" "$tmp"; then
    printf 'unchanged %s\n' "$target"
    exit 0
else
    result=$?
    [ "$result" -eq 1 ] || exit "$result"
fi
if [ -s "$tmp" ] && [ -n "$(tail -c 1 "$tmp")" ]; then
    printf '\n' >> "$tmp"
fi
printf '%s\n' "$line" >> "$tmp"
cp -p "$tmp" "$lock/expected"
if [ "$existed" -eq 1 ]; then
    cmp -s "$target" "$backup" || { echo 'configuration changed during append' >&2; exit 1; }
else
    [ ! -e "$target" ] && [ ! -L "$target" ] || { echo 'configuration appeared during append' >&2; exit 1; }
fi
mv -f "$tmp" "$target"
cmp -s "$target" "$lock/expected" || { echo 'configuration verification failed; backup retained' >&2; exit 1; }
printf 'applied %s\n' "$target"
'''
    return script.replace('__PATH__', _sq(path)).replace('__LINE__', _sq(line))


def _windows_append_line_command(definition: ConfigModuleDefinition, line: str) -> str:
    relative = definition.path.removeprefix('~/').replace('/', '\\').replace("'", "''")
    path_expr = "$PROFILE" if definition.id == 'powershell_profile' else f"(Join-Path $env:USERPROFILE '{relative}')"
    script = r'''$ErrorActionPreference = 'Stop'
$tmp = $null
$lockStream = $null
try {
    $p = __PATH__
    $line = __LINE__
    $dir = Split-Path -Parent $p
    if (-not (Test-Path -LiteralPath $dir)) { [IO.Directory]::CreateDirectory($dir) | Out-Null }
    $lockPath = $p + '.wsl-ops-lock'
    $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $exists = Test-Path -LiteralPath $p
    $original = [byte[]]@()
    $content = ''
    $encoding = New-Object Text.UTF8Encoding($false, $true)
    if ($exists) {
        if (Test-Path -LiteralPath $p -PathType Container) { throw 'target is a directory' }
        if ((Get-Item -LiteralPath $p).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'linked configuration requires direct target selection' }
        $original = [IO.File]::ReadAllBytes($p)
        $stream = New-Object IO.MemoryStream(,$original)
        $reader = New-Object IO.StreamReader($stream, $encoding, $true)
        try { $content = $reader.ReadToEnd(); $encoding = $reader.CurrentEncoding } finally { $reader.Dispose() }
    }
    if (($content -split '\r\n|\n|\r') -ccontains $line) { Write-Output "unchanged $p"; exit 0 }
    $newline = "`n"
    if ($content.Contains("`r`n") -or $content.Length -eq 0) { $newline = "`r`n" }
    $suffix = ''
    if ($content.Length -gt 0 -and -not ($content.EndsWith("`n") -or $content.EndsWith("`r"))) { $suffix = $newline }
    $suffix += $line + $newline
    $expected = [byte[]]($original + $encoding.GetBytes($suffix))
    $tmp = Join-Path $dir ('.wsl-ops-tmp-' + [Guid]::NewGuid().ToString('N'))
    [IO.File]::WriteAllBytes($tmp, $expected)
    if ($exists) {
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($p)) -cne [Convert]::ToBase64String($original)) { throw 'configuration changed during append' }
        do { $backup = $p + '.wsl-ops-bak-' + [Guid]::NewGuid().ToString('N') } while (Test-Path -LiteralPath $backup)
        [IO.File]::Replace($tmp, $p, $backup)
    } else {
        [IO.File]::Move($tmp, $p)
    }
    $tmp = $null
    if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($p)) -cne [Convert]::ToBase64String($expected)) { throw 'configuration verification failed; backup retained' }
    Write-Output "applied $p"
} catch {
    [Console]::Error.WriteLine('configuration append failed: ' + $_.Exception.Message)
    exit 1
} finally {
    try {
        if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-Item -LiteralPath $tmp -Force }
    } finally {
        if ($lockStream) { $lockStream.Dispose(); [IO.File]::Delete($lockPath) }
    }
}
'''
    script = script.replace('__PATH__', path_expr).replace('__LINE__', "'" + line.replace("'", "''") + "'")
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    return f'powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}'


def _sq(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"
