#!/bin/sh
# Render in isolated HOME; Chromium only talks to intercepted fixture responses.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
RUN=$(mktemp -d "${TMPDIR:-/tmp}/wsl-agent-browser-XXXXXX")
trap 'rm -rf -- "$RUN"' EXIT
export ARCHITECTURE_REVIEW_OUTPUT="$RUN"
"$ROOT/scripts/test_isolated.sh" tests/browser/agent_fixture.py
node "$ROOT/tests/browser/agent-client-state.cjs"
