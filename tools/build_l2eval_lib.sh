#!/usr/bin/env bash
# Deprecated: use build_l2_lib.sh.  This shim exists so existing scripts
# and CI configurations that reference the old name keep working.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "[warn] build_l2eval_lib.sh is deprecated; use build_l2_lib.sh" >&2
exec "${DIR}/build_l2_lib.sh" "$@"
