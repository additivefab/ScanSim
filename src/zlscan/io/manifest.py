"""File manifests: relative path, size and SHA-256 of every file under a directory.

Used to freeze the legacy packages and, later, to record the inputs and outputs of formal
runs. Standard library only.

A manifest file named ``MANIFEST_NAME`` lives in the directory it describes and never lists
itself. Editor and interpreter noise (``.DS_Store``, ``__pycache__``, ``*.pyc``) is ignored so
that running Python never makes a frozen directory look modified.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

FORMAT = "zlscan-manifest/1"
MANIFEST_NAME = "MANIFEST.json"

_IGNORED_NAMES = frozenset({".DS_Store"})
_IGNORED_DIRS = frozenset({"__pycache__"})
_IGNORED_SUFFIXES = (".pyc",)


def _is_ignored(relative: Path) -> bool:
    if relative.name in _IGNORED_NAMES or relative.name.endswith(_IGNORED_SUFFIXES):
        return True
    return any(part in _IGNORED_DIRS for part in relative.parts)


def sha256_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    """Relative paths of all regular files under root, sorted by their POSIX form."""
    found = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative == Path(MANIFEST_NAME) or _is_ignored(relative):
            continue
        found.append(relative)
    return sorted(found, key=lambda p: p.as_posix())


def build_manifest(root: Path) -> dict[str, Any]:
    root = Path(root)
    entries = [
        {
            "path": relative.as_posix(),
            "size": (root / relative).stat().st_size,
            "sha256": sha256_file(root / relative),
        }
        for relative in _files(root)
    ]
    return {"format": FORMAT, "files": entries}


def write_manifest(root: Path) -> Path:
    """Write ``root/MANIFEST_NAME`` and return its path."""
    root = Path(root)
    target = root / MANIFEST_NAME
    text = json.dumps(build_manifest(root), ensure_ascii=False, indent=1)
    target.write_text(text + "\n", encoding="utf-8")
    return target


def load_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise ValueError(f"unsupported manifest format {data.get('format')!r}, expected {FORMAT!r}")
    return data


def verify_manifest(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Compare a directory with a manifest. Returns problems; an empty list means identical."""
    root = Path(root)
    expected = {entry["path"]: entry for entry in manifest["files"]}
    actual = {relative.as_posix() for relative in _files(root)}
    problems = [f"extra: {path}" for path in sorted(actual - expected.keys())]
    for path, entry in sorted(expected.items()):
        target = root / path
        if not target.is_file():
            problems.append(f"missing: {path}")
        elif target.stat().st_size != entry["size"] or sha256_file(target) != entry["sha256"]:
            problems.append(f"modified: {path}")
    return sorted(problems, key=lambda line: (line.split(": ", 1)[1], line))
