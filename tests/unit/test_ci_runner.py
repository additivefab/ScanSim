"""CI must preserve failed process evidence, use all targets, and validate backend artifacts."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from tools.ci_runner import CICommands, extract_backend


def test_failing_command_records_actual_exit_and_log(tmp_path):
    runner = CICommands(tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        runner.run(
            "fail", [sys.executable, "-c", 'print("actual failure output"); raise SystemExit(7)']
        )
    commands = json.loads((tmp_path / "commands.json").read_text())
    assert commands[0]["returncode"] == 7
    assert "actual failure output" in (tmp_path / "fail.log").read_text()


def test_backend_archive_hash_is_checked_before_unpack(tmp_path):
    source = tmp_path / "repo"
    (source / "install/locks").mkdir(parents=True)
    archive = source / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        body = b"{}"
        m = tarfile.TarInfo("bundle/build_manifest.json")
        m.size = len(body)
        tar.addfile(m, io.BytesIO(body))
    config = source / "install/locks/ci-build.json"
    config.write_text(
        json.dumps(
            dict(
                format="zlscan.ci-build-input/1",
                bundle=archive.name,
                sha256="0" * 64,
                prefix="bundle",
            )
        )
    )
    with pytest.raises(ValueError, match="SHA256"):
        extract_backend(source, tmp_path / "work")
    assert not (tmp_path / "work").exists()


def test_workflow_covers_minimum_and_current_python_on_all_three_platforms():
    root = Path(__file__).resolve().parents[2]
    w = json.loads((root / ".github/workflows/ci.yml").read_text())
    job = w["jobs"]["verify"]
    assert set(job["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "windows-latest",
        "macos-latest",
    }
    assert set(job["strategy"]["matrix"]["python"]) == {"3.11", "3.14"}
    assert job["strategy"]["fail-fast"] is False
    assert w["permissions"] == {"contents": "read"}
    artifact = job["steps"][-1]
    assert artifact["if"] == "always()"
    assert artifact["with"]["if-no-files-found"] == "error"


def test_backend_selection_excludes_macos_runtime_wheels(tmp_path):
    from zipfile import ZipFile

    from tools.audit_wheel_install import sha

    source = tmp_path / "repo"
    (source / "install/locks").mkdir(parents=True)
    whl = tmp_path / "example-1.0-py3-none-any.whl"
    with ZipFile(whl, "w") as z:
        z.writestr("example-1.0.dist-info/METADATA", "Name: example\nVersion: 1.0\n")
    manifest = {"example": dict(filename=whl.name, version="1.0", sha256=sha(whl))}
    archive = source / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, body in [
            ("bundle/build_manifest.json", json.dumps(manifest).encode()),
            ("bundle/build_wheelhouse/" + whl.name, whl.read_bytes()),
            ("bundle/wheelhouse/mac-runtime.whl", b"wrong target"),
        ]:
            m = tarfile.TarInfo(name)
            m.size = len(body)
            tar.addfile(m, io.BytesIO(body))
    (source / "install/locks/ci-build.json").write_text(
        json.dumps(
            dict(
                format="zlscan.ci-build-input/1",
                bundle=archive.name,
                sha256=sha(archive),
                prefix="bundle",
            )
        )
    )
    house, path = extract_backend(source, tmp_path / "work")
    assert [p.name for p in house.iterdir()] == [whl.name]
    assert json.loads(path.read_text()) == manifest


def test_workflow_shell_scripts_contain_real_newlines():
    root = Path(__file__).resolve().parents[2]
    w = json.loads((root / ".github/workflows/ci.yml").read_text())
    for step in w["jobs"]["verify"]["steps"]:
        assert chr(92) + "n" not in step.get("run", "")
