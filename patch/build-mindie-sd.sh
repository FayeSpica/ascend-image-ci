#!/usr/bin/env bash
# Build the MindIE-SD wheel from source and drop it in /out. Runs in the builder
# stage of patch/Dockerfile.mindie-sd; nothing here lands in the final image
# except the wheel it produces.
#
# Kept as a script (rather than an inline RUN) because the sed expression below
# is full of quoting that does not survive a Dockerfile one-liner intact.
#
#   $1 - git repo to clone (e.g. https://gitcode.com/Ascend/MindIE-SD.git)
#   $2 - ref to check out; empty means the repo's default branch
set -euo pipefail

REPO="${1:?repo URL required}"
REF="${2:-}"
SRC_DIR=/tmp/MindIE-SD

# git is not guaranteed to be present in the base image.
if ! command -v git >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends git
    rm -rf /var/lib/apt/lists/*
fi

# The ops build inside setup.py needs the CANN toolchain on PATH/LD_LIBRARY_PATH.
for env_script in /usr/local/Ascend/ascend-toolkit/set_env.sh \
                  /usr/local/Ascend/nnal/atb/set_env.sh; do
    if [ -f "$env_script" ]; then
        # shellcheck disable=SC1090
        . "$env_script"
    fi
done

rm -rf "$SRC_DIR"
if [ -n "$REF" ]; then
    git clone --depth 1 --branch "$REF" "$REPO" "$SRC_DIR"
else
    git clone --depth 1 "$REPO" "$SRC_DIR"
fi
cd "$SRC_DIR"

# Comment out the tik_ops build step: it is not needed for this image and is by
# far the slowest part of the build. Assert the line exists first so an upstream
# rename fails here loudly instead of silently reinstating a long ops build.
OPS_SCRIPT=build/build_ops.sh
grep -qE '^[[:space:]]*source .*build_tik_ops\.sh' "$OPS_SCRIPT"
sed -i -E 's|^([[:space:]]*)(source .*build_tik_ops\.sh)|\1# \2|' "$OPS_SCRIPT"

# bdist_wheel needs setuptools' wheel backend; not always installed in the base.
python3 -m pip install --no-cache-dir wheel

python3 setup.py bdist_wheel

# Exactly one wheel is expected; a glob COPY in the final stage would silently
# take whatever is here, so fail now if the build produced 0 or >1.
mkdir -p /out
count=$(find dist -maxdepth 1 -name 'mindiesd-*.whl' | wc -l)
if [ "$count" -ne 1 ]; then
    echo "expected exactly 1 mindiesd wheel in dist/, found ${count}" >&2
    ls -l dist >&2 || true
    exit 1
fi
cp dist/mindiesd-*.whl /out/
ls -l /out
