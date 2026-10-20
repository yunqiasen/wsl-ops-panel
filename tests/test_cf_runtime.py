"""Exercise the detached entry point with a real child process, without systemd/CF."""

import json
from pathlib import Path
import subprocess
import sys

from app.services.cf_tunnels import runner


def make_cloudflared(tmp_path):
    binary = tmp_path / "cloudflared"
    binary.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, sys, time
# Cloudflared declares these flags on its tunnel subcommand, not globally.
if sys.argv[1:3] != ["tunnel", "--no-autoupdate"]:
    raise SystemExit(2)
root = pathlib.Path(os.environ["HOME"])
(root / "child.json").write_text(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
print("https://detached-fixture.trycloudflare.com", flush=True)
try:
    while True: time.sleep(.05)
finally:
    (root / "child-stopped").write_text("yes")
""")
    binary.chmod(0o700)
    return binary


def test_unverified_detached_runner_expires_and_reaps_its_child(tmp_path):
    binary = make_cloudflared(tmp_path)
    directory = tmp_path / "private-run"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(runner.__file__)),
            "--directory",
            str(directory),
            "--token",
            "fixture-token",
            "--binary",
            str(binary),
            "--origin",
            "http://127.0.0.1:18080",
            "--startup-seconds",
            "0.3",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 124, result.stderr
    assert (directory / "child-stopped").read_text() == "yes"
    assert json.loads((directory / "url.json").read_text()) == {
        "token": "fixture-token",
        "url": "https://detached-fixture.trycloudflare.com",
    }
    child = json.loads((directory / "child.json").read_text())
    assert child["env"]["HOME"] == str(directory)
    assert child["argv"][-1] == "http://127.0.0.1:18080"
