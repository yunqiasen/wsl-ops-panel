"""A detached guardian: unverified tunnels expire even if the panel disappears.

This entry point deliberately uses only the standard library and absolute paths.
"""

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time


def run(
    directory: Path, token: str, binary: str, origin: str, startup_seconds: float = 105
) -> int:
    os.umask(0o077)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = directory / "cloudflared.yml"
    config.write_text("{}\n")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(directory),
        "LANG": "C.UTF-8",
    }
    process = subprocess.Popen(
        [
            binary,
            "tunnel",
            "--no-autoupdate",
            "--config",
            str(config),
            "--protocol",
            "http2",
            "--url",
            origin,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + startup_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = b""
    log = directory / "cloudflared.log"
    try:
        while not stopped and process.poll() is None:
            verified = (directory / "verified").exists() and (
                directory / "verified"
            ).read_text() == token
            if not verified and time.monotonic() >= deadline:
                return 124
            for key, _ in selector.select(0.1):
                part = os.read(key.fileobj.fileno(), 8192)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                buffer = (buffer + part)[-16384:]
                match = re.search(
                    rb"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com", buffer
                )
                if match:
                    temporary = directory / "url.tmp"
                    temporary.write_text(
                        json.dumps({"token": token, "url": match[0].decode()})
                    )
                    temporary.replace(directory / "url.json")
                if log.exists() and log.stat().st_size + len(part) > 262144:
                    log.replace(directory / "cloudflared.previous.log")
                with log.open("ab") as output:
                    output.write(part)
        return process.returncode or 0
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        process.stdout.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--startup-seconds", type=float, default=105)
    args = parser.parse_args()
    raise SystemExit(
        run(args.directory, args.token, args.binary, args.origin, args.startup_seconds)
    )
