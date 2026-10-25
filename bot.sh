#!/usr/bin/env bash
set -euo pipefail

BOT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
BOT_PYTHON="$BOT_ROOT/venv/bin/python"
if [[ ! -x "$BOT_PYTHON" ]]; then
    BOT_PYTHON="python3"
fi
exec "$BOT_PYTHON" "$BOT_ROOT/scripts/botctl.py" "$@"
