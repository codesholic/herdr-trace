#!/bin/sh
# Entry point for herdr's plugin commands. herdr runs them in the plugin folder with herdr's
# environment, not the user's shell, so Python is looked for on PATH and in the usual places.
set -eu
ROOT=${HERDR_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
for candidate in ${HERDR_TRACE_PYTHON:-} python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON=$candidate
    break
  fi
done
if [ -z "${PYTHON:-}" ]; then
  echo "herdr-trace needs Python 3.11 or later; set HERDR_TRACE_PYTHON to its path" >&2
  exit 1
fi
if [ "${1:-}" = check ]; then
  echo "herdr-trace will run with $("$PYTHON" --version 2>&1)"
  exit 0
fi
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PYTHON" -m herdr_trace plugin "$@"
