#!/bin/sh
# Real isolated panel + real browser; only Docker, user manager and HTTP origins are substituted.
set -eu
ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
exec bash "$ROOT/scripts/test_isolated.sh" tests/browser/cf_fixture.py -s
