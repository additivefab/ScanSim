"""Inventory the added module documents and read the EDA project without modifying it."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from zlscan.io.manifest import build_manifest, sha256_file


def audit(root: Path) -> dict:
    documents = root / "AD9268模块资料V4.1"
    project = root / "扫描底_zl2.eprj2"
    before = sha256_file(project)
    # Deliberately do not export history payloads, user IDs, encryption keys or IVs.
    tables = (
        "schematics",
        "documents",
        "devices",
        "components",
        "texts",
        "resources",
        "project_structures",
        "history_data",
    )
    with sqlite3.connect(project.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        counts = {t: conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}
        structure = json.loads(
            conn.execute(
                "SELECT structure FROM project_structures ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
        structure = {
            key: structure[key]
            for key in ("boards", "schematics", "sheets", "pcbs")
            if key in structure
        }
    after = sha256_file(project)
    if before != after:
        raise RuntimeError("source project changed during inspection")
    return {
        "format": "zlscan-added-hardware-sources/1",
        "source_root": str(root.resolve()),
        "documents_directory": documents.name,
        "documents_manifest": build_manifest(documents),
        "project": {
            "path": project.name,
            "size": project.stat().st_size,
            "sha256": before,
            "sqlite_integrity": integrity,
            "source_unchanged": True,
            "table_counts": counts,
            "latest_structure": structure,
            "circuit_body_status": "not_decoded",
            "note": "Structure metadata is not an electrical netlist or wiring verification.",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "references/hardware_20261112",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Indexed {len(result['documents_manifest']['files'])} files; project unchanged")
