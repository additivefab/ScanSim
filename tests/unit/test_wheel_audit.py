"""A wheel must contain exact Python sources; offline input labels cannot hide altered bytes."""

import hashlib
import json
from zipfile import ZipFile

import pytest
from tools.audit_wheel_install import inspect_wheel, validate_wheelhouse


def wheel(path, *, name="example", version="1.0", modules=None):
    with ZipFile(path, "w") as z:
        z.writestr(f"{name}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n")
        z.writestr(f"{name}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\n")
        for file, content in (modules or {}).items():
            z.writestr(file, content)


def test_all_new_python_modules_are_required_even_if_assets_exist(tmp_path):
    p = tmp_path / "zlscan.whl"
    modules = {"zlscan/api/static/index.html": b"HTML", "zlscan/ident/timing.py": b"code"}
    expected = dict(
        version="1.0",
        assets={"zlscan/api/static/index.html": hashlib.sha256(b"HTML").hexdigest()},
        python_sources={"zlscan/ident/timing.py": hashlib.sha256(b"code").hexdigest()},
    )
    wheel(p, name="zlscan", modules=modules)
    assert inspect_wheel(p, expected)["python_modules"] == 1
    wheel(p, name="zlscan", modules={"zlscan/api/static/index.html": b"HTML"})
    with pytest.raises(AssertionError, match="Python"):
        inspect_wheel(p, expected)
    wheel(p, name="zlscan", modules={**modules, "zlscan/ident/timing.py": b"changed"})
    with pytest.raises(AssertionError):
        inspect_wheel(p, expected)


def manifest(tmp_path):
    p = tmp_path / "example-1.0-py3-none-any.whl"
    wheel(p)
    m = tmp_path / "manifest.json"
    m.write_text(
        json.dumps(
            {
                "example": dict(
                    filename=p.name,
                    version="1.0",
                    sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                )
            }
        )
    )
    return p, m


def test_input_hash_and_metadata_must_both_match(tmp_path):
    p, m = manifest(tmp_path)
    assert validate_wheelhouse(tmp_path, m)["example"]["version"] == "1.0"
    d = json.loads(m.read_text())
    d["example"]["version"] = "2.0"
    m.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="metadata"):
        validate_wheelhouse(tmp_path, m)
    d["example"]["version"] = "1.0"
    m.write_text(json.dumps(d))
    p.write_bytes(b"altered")
    with pytest.raises(ValueError, match="SHA256"):
        validate_wheelhouse(tmp_path, m)


def test_manifest_cannot_escape_house_or_replace_current_project(tmp_path):
    p, m = manifest(tmp_path)
    d = json.loads(m.read_text())
    d["example"]["filename"] = "../outside.whl"
    m.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="filename"):
        validate_wheelhouse(tmp_path, m)
    wheel(p, name="zlscan")
    d = {
        "zlscan": dict(
            filename=p.name, version="1.0", sha256=hashlib.sha256(p.read_bytes()).hexdigest()
        )
    }
    m.write_text(json.dumps(d))
    with pytest.raises(ValueError, match="project"):
        validate_wheelhouse(tmp_path, m)


def test_snapshot_is_bound_to_fixed_commit_not_mutable_worktree(tmp_path):
    import shutil
    import subprocess

    from tools.audit_wheel_install import verify_commit_snapshot

    source = tmp_path / "repo"
    source.mkdir()
    path = source / "source name.py"
    path.write_text("value = 1\n")
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(["git", *args], cwd=source, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    shutil.copyfile(path, snapshot / path.name)
    verify_commit_snapshot(source, snapshot, commit)
    path.write_text("value = 2\n")
    shutil.copyfile(path, snapshot / path.name)
    with pytest.raises(ValueError, match="differs from commit"):
        verify_commit_snapshot(source, snapshot, commit)


def test_partial_offline_inputs_cannot_fall_back_to_network(tmp_path):
    from tools.audit_wheel_install import audit

    output = tmp_path / "output"
    with pytest.raises(ValueError, match="requires both"):
        audit(tmp_path, output, build_wheelhouse=tmp_path)
    assert not output.exists()
