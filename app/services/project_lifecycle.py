"""Project deletion: preflight all targets before stopping anything."""

import json
import re
from pathlib import Path
import shutil
import subprocess
import sys

from app.services.docker_lifecycle import _directory_removal


def execute(context, action, *, runner=subprocess.run):
    if action not in {"delete", "full_delete"}:
        raise ValueError("unsupported project lifecycle action")
    path = Path(context["project_dir"]).absolute()
    current_stat = path.lstat() if path.exists() or path.is_symlink() else None
    current_identity = (
        [current_stat.st_dev, current_stat.st_ino] if current_stat else None
    )
    if (
        "directory_identity" in context
        and context["directory_identity"] != current_identity
    ):
        raise ValueError("项目目录已变化，请刷新后重试")
    removal = _directory_removal(path, runner) if action == "full_delete" else None
    identity = current_identity
    groups = unit_groups(context)

    def run(command):
        result = runner(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(f"project command failed (exit={result.returncode})")
        return result.stdout

    # Preflight every unit/scope and exact privileged command before mutations.
    for scope, units in groups:
        control = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
        for unit in units:
            result = run(
                [
                    *control,
                    "show",
                    unit,
                    "-p",
                    "WorkingDirectory",
                    "-p",
                    "LoadState",
                    "-p",
                    "ExecStart",
                ]
            )
            fields = dict(
                line.split("=", 1) for line in result.splitlines() if "=" in line
            )
            cwd = Path(fields.get("WorkingDirectory") or "/").resolve()
            script_in_project = re.search(
                r"(?:^|[=\s])" + re.escape(str(path)) + r"(?:/|[\s;}]|$)", fields.get("ExecStart", "")
            )
            if fields.get("LoadState") != "loaded" or not (
                cwd.is_relative_to(path) or script_in_project
            ):
                raise ValueError("项目运行项归属已变化，请刷新后重试")
        if scope == "system":
            for verb in ("stop", "disable"):
                run(["sudo", "-n", "-l", "--", *control, verb, *units])
    for scope, units in groups:
        control = ["systemctl", "--user"] if scope == "user" else ["systemctl"]
        mutate = control if scope == "user" else ["sudo", "-n", *control]
        run([*mutate, "stop", *units])
        for unit in units:
            result = runner(
                [*control, "is-active", unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 3 or result.stdout.strip() not in {
                "inactive",
                "failed",
            }:
                raise RuntimeError("项目运行项尚未停止，目录保留")
        run([*mutate, "disable", *units])
        for unit in units:
            result = runner(
                [*control, "is-enabled", unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode not in {0, 1} or result.stdout.strip() not in {
                "disabled",
                "static",
                "indirect",
                "masked",
            }:
                raise RuntimeError("项目自启关闭核验失败，目录保留")
    if removal:
        final_stat = path.lstat() if path.exists() or path.is_symlink() else None
        if final_stat is None or [final_stat.st_dev, final_stat.st_ino] != identity:
            raise ValueError("项目目录已变化，停止删除")
        # Repeat path, mount and permission checks immediately before removal.
        removal = _directory_removal(path, runner)
        if removal[0] == "rmtree":
            shutil.rmtree(path)
        else:
            run(removal)
        if path.exists():
            raise RuntimeError("项目目录删除核验失败")
    print(
        "项目运行项已清理"
        + ("，目录已删除" if action == "full_delete" else "，源码目录保留")
    )


def unit_groups(context):
    targets = context.get("service_targets") or [
        dict(name=name, scope=context.get("service_scope", "system"))
        for name in context.get("service_units", [])
    ]
    groups = {}
    for target in targets:
        name, scope = target.get("name"), target.get("scope")
        if (
            not isinstance(name, str)
            or name.startswith("-")
            or not name.endswith(".service")
            or scope not in {"user", "system"}
        ):
            raise ValueError("invalid project service target")
        if name not in groups.setdefault(scope, []):
            groups[scope].append(name)
    return list(groups.items())


def main():
    try:
        execute(json.loads(sys.argv[1]), sys.argv[2])
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
