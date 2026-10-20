"""Legacy CF tunnel adapter for externally-managed systemd units.

Read-only inspection finds existing cloudflared systemd tunnels that are
explicitly associated with a project.  The runtime adapter controls them
without creating new resources, disabling, or unmasking.  All operations
verify the unit's explicit association before acting.
"""

from __future__ import annotations

import os
import ipaddress
import shlex
from pathlib import Path
import re
import subprocess
from time import monotonic
from typing import Any
from urllib.parse import urlparse

from app.models.assets import AssetSnapshot
from app.services.cf_tunnels.models import CFError
from app.services.cf_tunnels.runtime import quick_url

_DEFAULT_UNIT_ROOTS: list[Path] = [
    Path("/etc/systemd/system"),
    Path("/lib/systemd/system"),
    Path.home() / ".config" / "systemd" / "user",
]

_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com(?![\w.-])")

_KNOWN_SCRIPT_PATHS: list[str] = ["scripts/cftunnel-start.sh"]
_KNOWN_DOMAIN_PATHS: list[str] = ["logs/cftunnel-domain.txt"]

_ACTIVE_JOB_STATES = frozenset({"active", "activating", "deactivating"})


# --------------------------------------------------------------------------- #
# ExecStart parsing                                                           #
# --------------------------------------------------------------------------- #


def _is_loopback_or_lan(host: str) -> bool:
    """Check whether *host* is loopback or a private/container-LAN address."""
    if host.lower().rstrip(".") == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_multicast and (address.is_loopback or address.is_private)


def _extract_url_from_argv(argv: list[str]) -> str | None:
    """Extract and validate a ``--url`` origin from an argv list.

    Supports ``--url VALUE`` and ``--url=VALUE``.  Rejects credentials,
    non-HTTP(S) schemes, and non-loopback/container-LAN addresses.
    """
    origin: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--url" and i + 1 < len(argv):
            origin = argv[i + 1]
            break
        if arg.startswith("--url="):
            origin = arg[len("--url=") :]
            break
    if origin is None:
        return None
    origin = origin.strip("'\"")
    if not origin.startswith(("http://", "https://")):
        return None
    if "@" in origin:
        return None
    parsed = urlparse(origin)
    host = parsed.hostname or ""
    if not host or not _is_loopback_or_lan(host):
        return None
    return origin


def _parse_exec_start_value(exec_start: str) -> dict[str, Any] | None:
    """Parse an ExecStart value from either a unit file or ``systemctl show``.

    Unit-file format: ``/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080``
    systemctl-show format:
    ``{ path=/usr/bin/cloudflared ; argv[]=/usr/bin/cloudflared tunnel --url http://127.0.0.1:8080 ; ... }``

    Returns ``{"binary": str, "argv": list[str], "is_cloudflared": bool, "origin": str | None}``
    or ``None`` if unparseable.
    """
    exec_start = exec_start.strip()
    if not exec_start:
        return None

    binary: str
    argv: list[str]

    if exec_start.startswith("{") and exec_start.endswith("}"):
        inner = exec_start[1:-1].strip()
        props: dict[str, str] = {}
        for chunk in inner.split(" ; "):
            chunk = chunk.strip()
            if "=" in chunk:
                key, _, value = chunk.partition("=")
                props[key.strip()] = value.strip()
        binary = props.get("path", "")
        argv_str = props.get("argv[]", "")
        if not binary or not argv_str:
            return None
        try:
            argv = shlex.split(argv_str)
        except ValueError:
            return None
    else:
        try:
            argv = shlex.split(exec_start)
        except ValueError:
            return None
        if not argv:
            return None
        binary = argv[0]

    is_cf = Path(binary).name == "cloudflared"
    origin = _extract_url_from_argv(argv) if is_cf else None

    return {
        "binary": binary,
        "argv": argv,
        "is_cloudflared": is_cf,
        "origin": origin,
    }


# --------------------------------------------------------------------------- #
# Unit-file parsing                                                           #
# --------------------------------------------------------------------------- #


def _scope_for_root(root: Path) -> str:
    return "user" if "user" in root.parts else "system"


def _parse_unit_file(path: Path) -> dict[str, str] | None:
    """Parse a systemd .service file.

    Returns ``{"_masked": True}`` for masked units (symlink to /dev/null),
    a dict of [Service] keys for normal units, or ``None`` on read error.
    """
    try:
        if path.is_symlink():
            if os.readlink(path) == "/dev/null":
                return {"_masked": True}
        text = path.read_text()
    except OSError:
        return None
    result: dict[str, str] = {}
    section: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if "=" in stripped and section == "Service":
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


def _deduplicate_by_priority(
    found: list[tuple[Path, str]],
) -> list[tuple[Path, str]]:
    """Remove lower-priority duplicates within the same (scope, unit_name)."""
    seen: dict[tuple[str, str], tuple[Path, str]] = {}
    for path, scope in found:
        key = (scope, path.name)
        if key not in seen:
            seen[key] = (path, scope)
    return list(seen.values())


# --------------------------------------------------------------------------- #
# Scanning                                                                    #
# --------------------------------------------------------------------------- #


def _find_unit_files(
    unit_name: str | None,
    unit_roots: list[Path],
) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for root in unit_roots:
        if not root.exists():
            continue
        scope = _scope_for_root(root)
        if unit_name:
            candidates = [root / unit_name]
            if not unit_name.endswith(".service"):
                candidates.append(root / f"{unit_name}.service")
        else:
            candidates = sorted(root.glob("*.service"))
        for candidate in candidates:
            if candidate.exists():
                found.append((candidate, scope))
    return found


def _scan_for_associated(
    project_dir: str,
    unit_roots: list[Path],
) -> list[tuple[Path, str, dict[str, str], dict[str, Any]]]:
    """Scan unit roots for units matching *project_dir* with cloudflared."""
    all_units = _deduplicate_by_priority(_find_unit_files(None, unit_roots))
    results: list[tuple[Path, str, dict[str, str], dict[str, Any]]] = []
    for path, scope in all_units:
        parsed = _parse_unit_file(path)
        if parsed is None or parsed.get("_masked"):
            continue
        working_dir = parsed.get("WorkingDirectory", "")
        if not working_dir:
            continue
        if Path(working_dir).resolve() != Path(project_dir).resolve():
            continue
        exec_info = _parse_exec_start_value(parsed.get("ExecStart", ""))
        if exec_info is None:
            continue
        has_cf = exec_info["is_cloudflared"] or any(
            "cloudflared" in arg for arg in exec_info["argv"]
        )
        if not has_cf:
            continue
        results.append((path, scope, parsed, exec_info))
    return results


def _find_script_clues(project_dir: str, metadata: dict[str, Any]) -> str | None:
    """Check only explicit metadata paths and known script/domain file paths.

    Returns a reason string if clues are found, ``None`` otherwise.
    """
    root = Path(project_dir)
    candidates: list[str] = []

    start_script = metadata.get("start_script") or metadata.get("cf_start_script")
    domain_file = metadata.get("domain_file") or metadata.get("cf_domain_file")
    if isinstance(start_script, str):
        candidates.append(start_script)
    if isinstance(domain_file, str):
        candidates.append(domain_file)

    candidates.extend(_KNOWN_SCRIPT_PATHS)
    candidates.extend(_KNOWN_DOMAIN_PATHS)

    for rel in candidates:
        path = root / rel
        if path.exists():
            return "仅发现 cloudflared 相关脚本/域名文件，缺少可核验 systemd 管理对象"
    return None


# --------------------------------------------------------------------------- #
# Public inspection                                                           #
# --------------------------------------------------------------------------- #


def inspect_legacy(
    asset: AssetSnapshot,
    *,
    unit_roots: list[Path] | None = None,
) -> dict[str, Any] | None:
    """Inspect an asset for a legacy externally-managed CF tunnel.

    Returns a dict with ``management="external"`` for a verifiable direct
    cloudflared unit, ``management="external_hint"`` when only scripts,
    domain files, or non-direct cloudflared units exist, or ``None`` when
    no legacy clues are found.
    """
    project_dir = asset.metadata.get("project_dir") or asset.metadata.get("path")
    if not isinstance(project_dir, str) or not project_dir:
        return None

    roots = unit_roots if unit_roots is not None else _DEFAULT_UNIT_ROOTS

    # 1. Explicit unit from metadata.
    capabilities = asset.metadata.get("capabilities") or {}
    cf_tunnel = capabilities.get("cf_tunnel") or {}
    explicit_unit = cf_tunnel.get("unit") or cf_tunnel.get("cftunnel_unit")

    if explicit_unit:
        found = _deduplicate_by_priority(_find_unit_files(explicit_unit, roots))
        scopes = {scope for _, scope in found}
        if len(found) > 1 and len(scopes) > 1:
            raise CFError(
                "legacy_ambiguous",
                "同名单元同时存在于 system 和 user scope",
            )
        if found:
            path, scope = found[0]
            parsed = _parse_unit_file(path)
            if parsed is not None:
                if parsed.get("_masked"):
                    return {
                        "management": "external",
                        "unit": path.name,
                        "scope": scope,
                        "load_state": "masked",
                        "fragment_path": str(path),
                        "project_dir": project_dir,
                    }
                exec_info = _parse_exec_start_value(parsed.get("ExecStart", ""))
                has_cf = exec_info is not None and (
                    exec_info["is_cloudflared"]
                    or any("cloudflared" in a for a in exec_info["argv"])
                )
                if (
                    exec_info is not None
                    and exec_info["is_cloudflared"]
                    and exec_info["origin"] is not None
                ):
                    working_dir = parsed.get("WorkingDirectory", "")
                    if (
                        working_dir
                        and Path(working_dir).resolve() == Path(project_dir).resolve()
                    ):
                        return {
                            "management": "external",
                            "unit": path.name,
                            "scope": scope,
                            "project_dir": project_dir,
                            "origin": exec_info["origin"],
                            "load_state": "loaded",
                            "fragment_path": str(path),
                            "exec_start": parsed.get("ExecStart", ""),
                            "working_directory": working_dir,
                        }
                    return {
                        "management": "external_hint",
                        "reason": "显式关联的旧隧道单元工作目录与项目不一致",
                        "unit": path.name,
                        "fragment_path": str(path),
                    }
                if has_cf:
                    return {
                        "management": "external_hint",
                        "reason": "旧单元 ExecStart 非直接 cloudflared 或源站参数不可核验",
                        "unit": path.name,
                        "fragment_path": str(path),
                    }
                # Not a CF unit — fall through to scanning.

    # 2. Scan all unit roots for matching WorkingDirectory + cloudflared.
    matched = _scan_for_associated(project_dir, roots)
    if len(matched) > 1:
        raise CFError(
            "legacy_ambiguous",
            f"发现 {len(matched)} 个旧 CF 隧道单元，归属不明确",
        )
    if matched:
        path, scope, parsed, exec_info = matched[0]
        if exec_info["is_cloudflared"] and exec_info["origin"] is not None:
            return {
                "management": "external",
                "unit": path.name,
                "scope": scope,
                "project_dir": project_dir,
                "origin": exec_info["origin"],
                "load_state": "loaded",
                "fragment_path": str(path),
                "exec_start": parsed.get("ExecStart", ""),
                "working_directory": parsed.get("WorkingDirectory", ""),
            }
        return {
            "management": "external_hint",
            "reason": "旧单元 ExecStart 非直接 cloudflared 或源站参数不可核验",
            "unit": path.name,
            "fragment_path": str(path),
        }

    # 3. No unit found — check known script / domain file clues.
    clue = _find_script_clues(project_dir, asset.metadata)
    if clue:
        return {"management": "external_hint", "reason": clue}

    return None


# --------------------------------------------------------------------------- #
# Runtime adapter                                                             #
# --------------------------------------------------------------------------- #


class LegacyTunnelRuntime:
    """Control a legacy externally-managed cloudflared systemd unit."""

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root

    @property
    def boot_id(self) -> str:
        return (self.proc_root / "sys/kernel/random/boot_id").read_text().strip()

    # -- systemd argv helpers ----------------------------------------------- #

    @staticmethod
    def _systemctl_argv(scope: str, *args: str) -> list[str]:
        if scope == "user":
            return ["systemctl", "--user", *args]
        return ["sudo", "-n", "systemctl", *args]

    @staticmethod
    def _journalctl_argv(
        scope: str,
        unit: str,
        invocation_id: str,
        *,
        lines: int = 200,
    ) -> list[str]:
        if scope == "user":
            return [
                "journalctl",
                "--user",
                f"--unit={unit}",
                f"_SYSTEMD_INVOCATION_ID={invocation_id}",
                f"-n{lines}",
                "--no-pager",
                "-o",
                "cat",
            ]
        return [
            "sudo",
            "-n",
            "journalctl",
            f"--unit={unit}",
            f"_SYSTEMD_INVOCATION_ID={invocation_id}",
            f"-n{lines}",
            "--no-pager",
            "-o",
            "cat",
        ]

    # -- subprocess wrapper ------------------------------------------------- #

    def _run(
        self,
        argv: list[str],
        *,
        timeout: float,
        stage: str = "legacy_runtime",
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise CFError(stage, "旧隧道操作超时") from None

    def _show(
        self,
        unit: str,
        scope: str,
        *,
        timeout: float = 5,
    ) -> dict[str, str]:
        result = self._run(
            self._systemctl_argv(
                scope,
                "show",
                unit,
                "--property=LoadState,ActiveState,MainPID,InvocationID,"
                "Description,FragmentPath,WorkingDirectory,ExecStart,Transient",
            ),
            timeout=timeout,
            stage="legacy_runtime",
        )
        if result.returncode:
            raise CFError("legacy_runtime", "旧隧道服务管理器访问失败")
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    # -- association verification ------------------------------------------- #

    @staticmethod
    def _verify_unit(
        expected: dict[str, Any],
        info: dict[str, str],
        *,
        check_invocation: bool = False,
    ) -> None:
        """Verify the live unit matches expected association."""
        load_state = info.get("LoadState", "")
        if load_state == "masked":
            raise CFError("legacy_masked", "旧隧道单元处于 masked 状态")
        if load_state == "not-found":
            raise CFError("legacy_identity", "旧隧道单元未找到")

        expected_fp = expected.get("fragment_path", "")
        actual_fp = info.get("FragmentPath", "")
        if (
            not expected_fp
            or not actual_fp
            or Path(actual_fp).resolve() != Path(expected_fp).resolve()
        ):
            raise CFError("legacy_identity", "旧隧道单元文件来源缺失或已变化")

        expected_wd = expected.get("project_dir", "")
        actual_wd = info.get("WorkingDirectory", "")
        if not actual_wd or Path(actual_wd).resolve() != Path(expected_wd).resolve():
            raise CFError("legacy_identity", "旧隧道单元归属已变化")

        exec_info = _parse_exec_start_value(info.get("ExecStart", ""))
        if exec_info is None or not exec_info["is_cloudflared"]:
            raise CFError("legacy_identity", "旧隧道单元 ExecStart 已变化")

        expected_origin = expected.get("origin", "")
        if exec_info["origin"] != expected_origin:
            raise CFError("legacy_origin", "旧隧道源站参数已变化")

        if check_invocation:
            expected_inv = expected.get("invocation_id", "")
            actual_inv = info.get("InvocationID", "")
            if not expected_inv or not actual_inv or expected_inv != actual_inv:
                raise CFError(
                    "legacy_identity",
                    "旧隧道 InvocationID 已变化，原资源保持原样",
                )

    # -- deadline helper ---------------------------------------------------- #

    @staticmethod
    def _remaining_or_raise(deadline: float, stage: str) -> float:
        """Return remaining time or raise CFError if <= 0."""
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CFError(stage, "旧隧道操作超时")
        return remaining

    # -- public API --------------------------------------------------------- #

    def start(
        self,
        owner: str,
        token: str,
        origin: str,
        *,
        timeout: float,
        legacy: dict,
    ) -> dict:
        if legacy.get("management") != "external":
            raise CFError("legacy_identity", "旧隧道缺少可核验的管理对象")
        if legacy.get("load_state") == "masked":
            raise CFError("legacy_masked", "旧隧道单元处于 masked 状态，不可启动")

        legacy_origin = legacy.get("origin")
        if legacy_origin and legacy_origin != origin:
            raise CFError("legacy_origin", "源站参数与旧隧道单元不一致")

        unit = legacy["unit"]
        scope = legacy["scope"]
        project_dir = legacy["project_dir"]
        fragment_path = legacy["fragment_path"]
        deadline = monotonic() + timeout

        info = self._show(
            unit,
            scope,
            timeout=min(5, self._remaining_or_raise(deadline, "legacy_runtime")),
        )
        load_state = info.get("LoadState", "")
        if load_state == "masked":
            raise CFError("legacy_masked", "旧隧道单元处于 masked 状态，不可启动")
        if load_state == "not-found":
            raise CFError("legacy_identity", "旧隧道单元未找到")
        self._verify_unit(legacy, info)

        # Already active → reuse existing InvocationID.
        if info.get("ActiveState") in _ACTIVE_JOB_STATES:
            invocation_id = info.get("InvocationID", "")
            if invocation_id:
                return {
                    "kind": "legacy",
                    "owner": owner,
                    "token": token,
                    "unit": unit,
                    "scope": scope,
                    "boot_id": self.boot_id,
                    "invocation_id": invocation_id,
                    "fragment_path": fragment_path,
                    "project_dir": project_dir,
                    "origin": origin,
                }

        # Start the unit.
        result = self._run(
            self._systemctl_argv(scope, "start", unit),
            timeout=self._remaining_or_raise(deadline, "legacy_runtime"),
            stage="legacy_runtime",
        )
        if result.returncode:
            raise CFError("legacy_runtime", "旧隧道服务启动失败")

        # Wait for activation and capture InvocationID.
        remaining = self._remaining_or_raise(deadline, "legacy_runtime")
        info = self._show(unit, scope, timeout=min(5, remaining))
        if info.get("ActiveState") not in _ACTIVE_JOB_STATES:
            err = CFError("legacy_runtime", "旧隧道启动后未进入活动状态")
            err.handle = {
                "unit": unit,
                "scope": scope,
                "boot_id": self.boot_id,
                "project_dir": project_dir,
            }
            raise err
        invocation_id = info.get("InvocationID", "")
        if not invocation_id:
            err = CFError("legacy_runtime", "旧隧道未获得 InvocationID")
            err.handle = {
                "unit": unit,
                "scope": scope,
                "boot_id": self.boot_id,
                "project_dir": project_dir,
            }
            raise err

        return {
            "kind": "legacy",
            "owner": owner,
            "token": token,
            "unit": unit,
            "scope": scope,
            "boot_id": self.boot_id,
            "invocation_id": invocation_id,
            "fragment_path": fragment_path,
            "project_dir": project_dir,
            "origin": origin,
        }

    def observe(self, handle: dict, *, timeout: float = 5) -> dict:
        if handle["boot_id"] != self.boot_id:
            return {"active": False, "url": None}

        deadline = monotonic() + timeout
        info = self._show(
            handle["unit"],
            handle["scope"],
            timeout=min(5, self._remaining_or_raise(deadline, "legacy_runtime")),
        )

        load_state = info.get("LoadState", "")
        if load_state == "not-found" or (
            load_state == "masked" and info.get("ActiveState") not in _ACTIVE_JOB_STATES
        ):
            return {"active": False, "url": None}

        self._verify_unit(handle, info, check_invocation=True)

        active = (
            info.get("ActiveState") in _ACTIVE_JOB_STATES
            and int(info.get("MainPID") or 0) > 0
        )

        if active and info.get("InvocationID") != handle["invocation_id"]:
            raise CFError(
                "legacy_identity",
                "旧隧道 InvocationID 已变化，原资源保持原样",
            )

        url = None
        if active:
            remaining = self._remaining_or_raise(deadline, "legacy_runtime")
            raw_url = self._read_url_from_journal(
                handle["unit"],
                handle["scope"],
                handle["invocation_id"],
                timeout=remaining,
            )
            if raw_url:
                try:
                    url = quick_url(raw_url)
                except CFError:
                    url = None

        return {"active": active, "url": url}

    def activate(self, handle: dict, *, timeout: float = 5) -> None:
        if not self.observe(handle, timeout=timeout)["active"]:
            raise CFError("legacy_runtime", "核验完成时旧隧道进程已退出")

    def stop(self, handle: dict, *, timeout: float = 15) -> None:
        if handle["boot_id"] != self.boot_id:
            return  # Different boot — nothing to stop.

        deadline = monotonic() + timeout

        info = self._show(
            handle["unit"],
            handle["scope"],
            timeout=min(5, self._remaining_or_raise(deadline, "legacy_cleanup")),
        )

        load_state = info.get("LoadState", "")
        if (
            load_state == "not-found"
            or info.get("ActiveState") not in _ACTIVE_JOB_STATES
        ):
            return  # No live job exists, including an already-stopped masked unit.

        self._verify_unit(handle, info, check_invocation=True)

        # Issue stop.
        result = self._run(
            self._systemctl_argv(handle["scope"], "stop", handle["unit"]),
            timeout=self._remaining_or_raise(deadline, "legacy_cleanup"),
            stage="legacy_cleanup",
        )
        if result.returncode:
            raise CFError("legacy_cleanup", "旧隧道服务停止失败")

        # Verify stopped — must confirm, never claim success without observation.
        remaining = self._remaining_or_raise(deadline, "legacy_cleanup")
        info = self._show(
            handle["unit"],
            handle["scope"],
            timeout=min(5, remaining),
        )
        if info.get("ActiveState") in _ACTIVE_JOB_STATES:
            raise CFError("legacy_cleanup", "旧隧道服务停止后仍处于活动状态")

    # -- journal URL reading ------------------------------------------------ #

    def _read_url_from_journal(
        self,
        unit: str,
        scope: str,
        invocation_id: str,
        *,
        timeout: float,
    ) -> str | None:
        result = self._run(
            self._journalctl_argv(scope, unit, invocation_id, lines=200),
            timeout=timeout,
            stage="legacy_runtime",
        )
        if result.returncode:
            return None
        output = result.stdout[-8192:]
        matches = _URL_RE.findall(output)
        return matches[-1] if matches else None
