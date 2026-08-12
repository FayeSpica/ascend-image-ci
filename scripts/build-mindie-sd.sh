#!/usr/bin/env bash
# Build the MindIE-SD wheel from source and drop it in $OUT_DIR.
#
# Runs *inside* a CANN container (see the "Build MindIE-SD wheel" workflow),
# because the ops build needs that image's CANN toolchain. Nothing here is meant
# to run on the host.
#
# Kept as a script rather than an inline docker-run command because the sed
# below is full of quoting that does not survive being nested in YAML.
#
#   $1 - git repo to clone (e.g. https://gitcode.com/Ascend/MindIE-SD.git)
#   $2 - ref to check out; empty means the repo's default branch
#   OUT_DIR - where to leave the wheel (default /out, bind-mounted by the caller)
set -euo pipefail

REPO="${1:?repo URL required}"
REF="${2:-}"
OUT_DIR="${OUT_DIR:-/out}"
SRC_DIR=/tmp/MindIE-SD

# The CANN images are minimal; git is not necessarily present.
if ! command -v git >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends git
    rm -rf /var/lib/apt/lists/*
fi

# The ops build inside setup.py needs the CANN toolchain on PATH/LD_LIBRARY_PATH.
#
# -e/-u are lifted while sourcing: these are vendor scripts written without them
# in mind. nnal/atb/set_env.sh probes the shell with a bare $ZSH_VERSION, which
# under `set -u` is an "unbound variable" fatal error.
set +eu
for env_script in /usr/local/Ascend/ascend-toolkit/set_env.sh \
                  /usr/local/Ascend/nnal/atb/set_env.sh; do
    if [ -f "$env_script" ]; then
        # shellcheck disable=SC1090
        . "$env_script"
    fi
done
set -eu

rm -rf "$SRC_DIR"
if [ -n "$REF" ]; then
    git clone --depth 1 --branch "$REF" "$REPO" "$SRC_DIR"
else
    git clone --depth 1 "$REPO" "$SRC_DIR"
fi
cd "$SRC_DIR"
echo "building $(git rev-parse HEAD) from ${REPO}"

# Comment out the tik_ops build step: it is not needed for this wheel and is by
# far the slowest part of the build. Assert the line exists first so an upstream
# rename fails here loudly instead of silently reinstating a long ops build.
#
# Note this does NOT narrow which chips the wheel supports: the AscendC ops built
# just above it target every compute unit in one pass (build_ops.sh's
# default_compute_unit='ascend910;ascend910b;ascend910_93;ascend950'), so one
# wheel covers both A2 (ascend910b) and A3 (ascend910_93).
OPS_SCRIPT=build/build_ops.sh
grep -qE '^[[:space:]]*source .*build_tik_ops\.sh' "$OPS_SCRIPT"
sed -i -E 's|^([[:space:]]*)(source .*build_tik_ops\.sh)|\1# \2|' "$OPS_SCRIPT"

# setup.py imports setuptools, and bdist_wheel needs its wheel backend. Neither
# is present: since Python 3.12 the stdlib no longer bundles distutils and new
# environments no longer get setuptools preinstalled, so the CANN py3.12 image
# ships pip only.
python3 -m pip install --no-cache-dir setuptools wheel

python3 setup.py bdist_wheel

# Exactly one wheel is expected; the caller uploads by glob, so fail now rather
# than publish an arbitrary one of several.
count=$(find dist -maxdepth 1 -name 'mindiesd-*.whl' | wc -l)
if [ "$count" -ne 1 ]; then
    echo "expected exactly 1 mindiesd wheel in dist/, found ${count}" >&2
    ls -l dist >&2 || true
    exit 1
fi

mkdir -p "$OUT_DIR"
cp dist/mindiesd-*.whl "$OUT_DIR/"
ls -l "$OUT_DIR"
