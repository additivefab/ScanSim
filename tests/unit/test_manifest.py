"""File manifests record relative path, size and SHA-256, and detect any later drift."""

import hashlib
import json

import pytest

from zlscan.io.manifest import (
    FORMAT,
    MANIFEST_NAME,
    build_manifest,
    load_manifest,
    verify_manifest,
    write_manifest,
)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(bytes(range(256)))
    (tmp_path / "sub" / "中文名.csv").write_bytes(b"x,y\n1,2\n")
    return tmp_path


def test_entries_are_sorted_posix_paths_with_correct_size_and_hash(tree):
    m = build_manifest(tree)
    assert m["format"] == FORMAT
    paths = [f["path"] for f in m["files"]]
    assert paths == sorted(paths)
    assert paths == ["a.txt", "sub/b.bin", "sub/中文名.csv"]
    by_path = {f["path"]: f for f in m["files"]}
    assert by_path["a.txt"]["size"] == 5
    assert by_path["a.txt"]["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert by_path["sub/b.bin"]["sha256"] == hashlib.sha256(bytes(range(256))).hexdigest()


def test_manifest_is_deterministic(tree):
    assert build_manifest(tree) == build_manifest(tree)


def test_noise_files_are_ignored(tree):
    (tree / ".DS_Store").write_bytes(b"x")
    (tree / "sub" / "__pycache__").mkdir()
    (tree / "sub" / "__pycache__" / "m.cpython-314.pyc").write_bytes(b"x")
    (tree / "stray.pyc").write_bytes(b"x")
    assert [f["path"] for f in build_manifest(tree)["files"]] == [
        "a.txt",
        "sub/b.bin",
        "sub/中文名.csv",
    ]


def test_manifest_file_does_not_list_itself(tree):
    write_manifest(tree)
    assert (tree / MANIFEST_NAME).is_file()
    assert MANIFEST_NAME not in [f["path"] for f in build_manifest(tree)["files"]]


def test_round_trip_and_clean_verify(tree):
    path = write_manifest(tree)
    assert verify_manifest(tree, load_manifest(path)) == []


def test_modified_file_is_reported(tree):
    path = write_manifest(tree)
    (tree / "a.txt").write_bytes(b"hellO")  # same size, different content
    problems = verify_manifest(tree, load_manifest(path))
    assert problems == ["modified: a.txt"]


def test_missing_file_is_reported(tree):
    path = write_manifest(tree)
    (tree / "a.txt").unlink()
    assert verify_manifest(tree, load_manifest(path)) == ["missing: a.txt"]


def test_extra_file_is_reported(tree):
    path = write_manifest(tree)
    (tree / "new.txt").write_bytes(b"x")
    assert verify_manifest(tree, load_manifest(path)) == ["extra: new.txt"]


def test_all_three_kinds_of_drift_are_reported_together(tree):
    path = write_manifest(tree)
    (tree / "a.txt").write_bytes(b"changed")
    (tree / "sub" / "b.bin").unlink()
    (tree / "new.txt").write_bytes(b"x")
    assert sorted(verify_manifest(tree, load_manifest(path))) == [
        "extra: new.txt",
        "missing: sub/b.bin",
        "modified: a.txt",
    ]


def test_unknown_format_version_is_rejected(tree):
    path = write_manifest(tree)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["format"] = "something-else/9"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        load_manifest(path)


def test_written_json_is_utf8_and_unescaped(tree):
    path = write_manifest(tree)
    assert "中文名.csv" in path.read_text(encoding="utf-8")
