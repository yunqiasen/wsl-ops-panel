#!/bin/sh
# Run tests against copied sources and a private HOME/data, never the live panel.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    printf '%s\n' '请先在项目目录创建 .venv 并安装开发依赖。' >&2
    exit 1
fi
RUN=$(mktemp -d "${TMPDIR:-/tmp}/wsl-ops-tests-XXXXXX")
trap 'rm -rf -- "$RUN"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$RUN/home"
cp -a "$ROOT/app" "$ROOT/tests" "$ROOT/config" "$ROOT/docs" "$ROOT/scripts" "$ROOT/pyproject.toml" "$ROOT/README.md" "$RUN/"
find "$RUN" -type d -name __pycache__ -prune -exec rm -rf {} +
cd "$RUN"
# Generated shell scripts also need the declared project dependencies, not user-site packages.
export PATH="$ROOT/.venv/bin:$PATH"
export HOME="$RUN/home" PYTHONPATH="$RUN" PYTHONDONTWRITEBYTECODE=1 WSL_TEST_ORIGINAL_ROOT="$ROOT"
if [ "$#" -eq 0 ]; then set -- tests; fi
"$PYTHON" - "$@" <<'PY'
import os
import sys

root = os.environ['WSL_TEST_ORIGINAL_ROOT']
args = [os.getcwd() + arg[len(root):] if arg.startswith(root + '/') else arg for arg in sys.argv[1:]]
os.execv(sys.executable, [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider', *args, '-q', '--tb=short'])
PY
