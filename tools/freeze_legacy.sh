#!/bin/sh
# Freeze the legacy inputs into legacy/ (task S0-T02). Kept as a provenance record.
#
# Sources are the delivery directories one level above this project:
#   ../                                   -> legacy/package_a   (selected items only)
#   ../ZL_OEM_SCANNER_SIM/                -> legacy/package_b   (everything except the V3 plan)
#   ../ZL_SCANNER_V3_REVIEW_20261111/     -> legacy/review      (tables, configs, scripts, one doc)
#   ../originals/                         -> legacy/originals
#
# It refuses to overwrite an existing legacy/ because frozen data is read-only.
# After running it, run tools/make_legacy_manifest.py.
set -eu -f

HERE=$(cd "$(dirname "$0")/.." && pwd)
SRC=$(cd "$HERE/.." && pwd)
LEG="$HERE/legacy"

if [ -e "$LEG/package_a" ] || [ -e "$LEG/package_b" ]; then
    echo "legacy/ is already frozen; refusing to overwrite" >&2
    exit 1
fi

# Noise that must never be frozen. set -f keeps the patterns from being globbed.
EX="--exclude=.DS_Store --exclude=__pycache__ --exclude=*.pyc --exclude=.pytest_cache"

A="$LEG/package_a"
mkdir -p "$A"
for f in README_先读.md run_demo.py requirements.txt requirements-tested.txt \
         run_linux.sh run_windows.bat MANIFEST_SHA256.txt; do
    cp -p "$SRC/$f" "$A/$f"
done
for d in config src scripts tests rtl spice results viewer measurement_templates references; do
    rsync -a $EX "$SRC/$d/" "$A/$d/"
done
# ../docs also holds the later plan documents, which are not legacy inputs.
rsync -a $EX "--exclude=仿真软件开发方案_*.md" "$SRC/docs/" "$A/docs/"

# Package B: everything except the V3 plan that was later written into its docs/.
rsync -a $EX "--exclude=/docs/仿真软件开发方案_V3.md" "$SRC/ZL_OEM_SCANNER_SIM/" "$LEG/package_b/"

R="$LEG/review"
mkdir -p "$R/docs"
for d in tables configs scripts; do
    rsync -a $EX "$SRC/ZL_SCANNER_V3_REVIEW_20261111/$d/" "$R/$d/"
done
cp -p "$SRC/ZL_SCANNER_V3_REVIEW_20261111/docs/项目资料审查记录.md" "$R/docs/"

rsync -a $EX "$SRC/originals/" "$LEG/originals/"
echo "frozen into $LEG"
