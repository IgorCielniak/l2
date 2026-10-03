#!/usr/bin/env bash
# Download a prebuilt libkeystone.so from the keystone-engine PyPI wheel
# and vendor it into tools/vendor/.  This avoids depending on a
# system-installed libkeystone (which no longer ships in mainline Debian
# / Ubuntu 24.04 repositories) and removes the need for the user to run
# `pip install keystone-engine`.
#
# The wheel is a plain zip archive containing libkeystone.so at
# keystone/libkeystone.so.  We only extract that single file.
#
# Env vars:
#   KS_WHEEL_URL   override download URL
#   KS_WHEEL_SHA256 optional integrity check (empty = skip)
#   FORCE=1        redownload even if the vendored copy already exists

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENDOR_DIR="${ROOT_DIR}/tools/vendor"
OUT_SO="${VENDOR_DIR}/libkeystone.so"

# manylinux1 x86_64 wheel from PyPI (keystone-engine 0.9.2).
: "${KS_WHEEL_URL:=https://files.pythonhosted.org/packages/01/5c/40ffbec589262f49ff7c463d96ff0bfab0fbd98d9d869c370a70853a13fb/keystone_engine-0.9.2-py2.py3-none-manylinux1_x86_64.whl}"
: "${KS_WHEEL_SHA256:=5a5316a34323620b1bba31dcfe9e4b4ca6f0c030e82fc7a151da7c8fbe81a379}"

if [[ -f "${OUT_SO}" && "${FORCE:-0}" != "1" ]]; then
    echo "[info] vendored keystone already present: ${OUT_SO}"
    exit 0
fi

mkdir -p "${VENDOR_DIR}"
tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

whl="${tmpdir}/keystone.whl"
echo "[info] downloading ${KS_WHEEL_URL}"
curl -fsSL -o "${whl}" "${KS_WHEEL_URL}"

if [[ -n "${KS_WHEEL_SHA256}" ]]; then
    echo "${KS_WHEEL_SHA256}  ${whl}" | sha256sum -c -
fi

# The wheel is a zip; extract only libkeystone.so.
if ! command -v unzip >/dev/null 2>&1; then
    echo "[error] 'unzip' is required to extract the keystone wheel" >&2
    exit 1
fi
unzip -j -o "${whl}" 'keystone/libkeystone.so' -d "${VENDOR_DIR}" >/dev/null
chmod 0755 "${OUT_SO}"

echo "[info] vendored ${OUT_SO} ($(stat -c%s "${OUT_SO}") bytes)"
