#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export L2_FORCE_QILING_VM=1

if [[ -n "${PYTHON:-}" ]]; then
	PY_CMD="$PYTHON"
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
	PY_CMD="$ROOT_DIR/.venv/bin/python"
else
	PY_CMD="python3"
fi

exec "$PY_CMD" main.py "$@"
