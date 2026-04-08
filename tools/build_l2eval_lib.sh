#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"

mkdir -p "${BUILD_DIR}"

cc -O2 -fPIC -DL2_AS_LIBRARY -DL2_SOURCE_ROOT=\"${ROOT_DIR}\" -c "${ROOT_DIR}/main.c" -o "${BUILD_DIR}/l2eval.o"
ar rcs "${BUILD_DIR}/libl2eval.a" "${BUILD_DIR}/l2eval.o"
cc -shared -o "${BUILD_DIR}/libl2eval.so" "${BUILD_DIR}/l2eval.o"

echo "[info] wrote ${BUILD_DIR}/libl2eval.a"
echo "[info] wrote ${BUILD_DIR}/libl2eval.so"
