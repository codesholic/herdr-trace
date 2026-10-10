#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
# Tests must never reach a real herdr session, even when run from inside a herdr pane.
for name in $(env | sed -n 's/^\(HERDR_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$name"; done
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" "${PYTHON:-python3}" -m unittest discover -s tests -p 'test_*.py' "$@"
