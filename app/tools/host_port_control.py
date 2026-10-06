import argparse
import json
import subprocess
import sys

from app.services.host_ownership import execute


def main():
    parser = argparse.ArgumentParser(description="Control a verified listening owner.")
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--snapshot", required=True)
    args = parser.parse_args()
    try:
        execute(json.loads(args.snapshot), args.action)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
