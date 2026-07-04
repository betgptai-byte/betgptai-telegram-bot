#!/usr/bin/env bash
set -euo pipefail

# Safe local test script.
# This intentionally does NOT start Telegram polling.

cd "$(dirname "$0")/.."

echo "Running BETGPTAI local compile checks..."

PYTHON_BIN="${PYTHON_BIN:-python}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python3"
fi

export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-.pycache_local}"
mkdir -p "$PYTHONPYCACHEPREFIX"

"$PYTHON_BIN" -m py_compile ./*.py

echo "✅ Local compile checks passed."
echo "Telegram polling was not started."
