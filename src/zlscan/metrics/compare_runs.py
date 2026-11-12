"""Compare two run manifests and their observable output hashes."""

from __future__ import annotations

import json
from pathlib import Path

from zlscan.io.manifest import sha256_file


def _artifact(first: Path, second: Path, relative: str) -> dict:
    hashes = [
        sha256_file(root / relative) if (root / relative).is_file() else None
        for root in (first, second)
    ]
    return {
        "path": relative,
        "first_sha256": hashes[0],
        "second_sha256": hashes[1],
        "equal": hashes[0] == hashes[1] if None not in hashes else None,
        "status": "compared" if None not in hashes else "missing_artifact",
    }


def compare_runs(first: Path, second: Path) -> dict:
    """Return exact manifest differences and raw stream equality."""
    left = json.loads((first / "run.json").read_text(encoding="utf-8"))
    right = json.loads((second / "run.json").read_text(encoding="utf-8"))
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ValueError("run manifests must be JSON objects")
    keys = sorted(set(left) | set(right))
    differences = {
        key: {"first": left.get(key), "second": right.get(key)}
        for key in keys
        if left.get(key) != right.get(key)
    }
    stages = {
        "raw": _artifact(first, second, "observable/raw.zlraw"),
        "integrity": _artifact(first, second, "observable/integrity.json"),
        "transport_losses": _artifact(first, second, "observable/losses.json"),
        "reconstruction": _artifact(first, second, "observable/recon.npz"),
        "digital_trace": _artifact(first, second, "digital.json"),
    }
    return {
        "first": str(first),
        "second": str(second),
        "manifest_differences": differences,
        "raw": stages["raw"],
        "stages": stages,
        "comparison": "exact_file_bytes",
        "note": "File equality does not establish physical alignment or numerical agreement.",
    }
