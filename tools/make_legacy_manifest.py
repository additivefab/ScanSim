"""Write legacy/MANIFEST.json (path, size, SHA-256 of every frozen file).

Run once after tools/freeze_legacy.sh. It refuses to overwrite an existing manifest: the
frozen set is read-only, so a changed manifest must be a deliberate, reviewed act.
"""

import sys
from pathlib import Path

from zlscan.io.manifest import MANIFEST_NAME, load_manifest, write_manifest

LEGACY = Path(__file__).resolve().parents[1] / "legacy"


def main() -> int:
    target = LEGACY / MANIFEST_NAME
    if target.exists():
        print(f"{target} already exists; refusing to overwrite", file=sys.stderr)
        return 1
    path = write_manifest(LEGACY)
    files = load_manifest(path)["files"]
    total_mb = sum(f["size"] for f in files) / 1e6
    print(f"wrote {path}: {len(files)} files, {total_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
