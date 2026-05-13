#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"

mkdir -p "${BUILD_DIR}"

# Build a static archive without L2_AS_LIBRARY so executables link against
# scalar-returning `l2_eval` for legacy behavior. The stack-returning `eval`
# ABI is provided by the shared library build.
cc -O2 -DL2_SOURCE_ROOT=\"${ROOT_DIR}\" -c "${ROOT_DIR}/main.c" -o "${BUILD_DIR}/l2eval_static.o"
rm -f "${BUILD_DIR}/libl2eval.a"
ar rcs "${BUILD_DIR}/libl2eval.a" "${BUILD_DIR}/l2eval_static.o"

# Build a shared object with L2_AS_LIBRARY so consumers that link the
# shared object get the stack-returning `eval` ABI.
cc -O2 -fPIC -DL2_AS_LIBRARY -DL2_SOURCE_ROOT=\"${ROOT_DIR}\" -c "${ROOT_DIR}/main.c" -o "${BUILD_DIR}/l2eval_shared.o"
cc -shared -o "${BUILD_DIR}/libl2eval.so" "${BUILD_DIR}/l2eval_shared.o"

echo "[info] wrote ${BUILD_DIR}/libl2eval.a"
echo "[info] wrote ${BUILD_DIR}/libl2eval.so"
