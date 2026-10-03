#!/usr/bin/env bash
# Build the L2 runtime library (libl2.{a,so}).
#
# Historical name: this script was previously called build_l2eval_lib.sh
# and produced libl2eval.{a,so}.  The library has since been renamed to
# libl2 as it now offers more than eval (compile(), runtime helpers,
# etc.).  For backwards compatibility, symlinks with the old name are
# still created alongside the new artifacts.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
VENDOR_DIR="${ROOT_DIR}/tools/vendor"

mkdir -p "${BUILD_DIR}"

# Ensure the vendored libkeystone.so is available so the JIT loader
# does not need a system-installed copy.  The fetch script is a no-op
# when the vendored file is already present.
if [[ ! -f "${VENDOR_DIR}/libkeystone.so" ]]; then
    if [[ "${L2_SKIP_KEYSTONE_FETCH:-0}" != "1" ]]; then
        bash "${ROOT_DIR}/tools/fetch_keystone.sh" \
            || echo "[warn] fetch_keystone.sh failed; JIT will be disabled" >&2
    fi
fi

# Static archive: no L2_AS_LIBRARY define, so consumers linking the
# archive get the scalar-returning `l2_eval` for legacy behavior.
cc -O2 -DL2_SOURCE_ROOT=\"${ROOT_DIR}\" -c "${ROOT_DIR}/main.c" -o "${BUILD_DIR}/l2_static.o"
rm -f "${BUILD_DIR}/libl2.a"
ar rcs "${BUILD_DIR}/libl2.a" "${BUILD_DIR}/l2_static.o"

# Shared object: L2_AS_LIBRARY defined so `eval` uses the stack-returning
# ABI intended for hosted runtimes.  Link against libdl for the runtime
# keystone-JIT loader (dlopen/dlsym); on glibc >= 2.34 this is a no-op
# but keeping it explicit makes the build portable.
cc -O2 -fPIC -DL2_AS_LIBRARY -DL2_SOURCE_ROOT=\"${ROOT_DIR}\" -c "${ROOT_DIR}/main.c" -o "${BUILD_DIR}/l2_shared.o"
cc -shared -o "${BUILD_DIR}/libl2.so" "${BUILD_DIR}/l2_shared.o" -ldl

# Backwards-compat symlinks so consumers that still reference libl2eval
# continue to link successfully.
rm -f "${BUILD_DIR}/libl2eval.a" "${BUILD_DIR}/libl2eval.so"
ln -s libl2.a  "${BUILD_DIR}/libl2eval.a"
ln -s libl2.so "${BUILD_DIR}/libl2eval.so"

# Copy the vendored libkeystone.so alongside the built libraries so
# libl2's runtime loader can find it via a sibling-directory probe
# (dladdr → same dir).  This lets binaries linked against libl2.so
# JIT-assemble without any system libkeystone install.
if [[ -f "${VENDOR_DIR}/libkeystone.so" ]]; then
    cp -f "${VENDOR_DIR}/libkeystone.so" "${BUILD_DIR}/libkeystone.so"
    echo "[info] vendored ${BUILD_DIR}/libkeystone.so"
fi

echo "[info] wrote ${BUILD_DIR}/libl2.a"
echo "[info] wrote ${BUILD_DIR}/libl2.so"
echo "[info] compat symlinks: libl2eval.a, libl2eval.so"
