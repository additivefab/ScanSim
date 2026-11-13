"""Record actual CI processes and audit a freshly built wheel on the current runner."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tomllib
from datetime import UTC, datetime
from email.parser import Parser
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from tools.audit_wheel_install import normalized, sha, validate_wheelhouse


class CICommands:
    def __init__(self, output: Path):
        self.output = output
        self.commands = []

    def run(self, label: str, argv: list[str], *, cwd: Path | None = None):
        print("CI: " + label, flush=True)
        started = datetime.now(UTC).isoformat()
        with (self.output / (label + ".log")).open("w", encoding="utf-8") as log:
            result = subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT)
        self.commands.append(
            dict(
                label=label,
                argv=argv,
                returncode=result.returncode,
                started_at=started,
                finished_at=datetime.now(UTC).isoformat(),
            )
        )
        (self.output / "commands.json").write_text(
            json.dumps(self.commands, indent=2) + "\n", encoding="utf-8"
        )
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv)


def extract_backend(source: Path, work: Path) -> tuple[Path, Path]:
    config = json.loads((source / "install/locks/ci-build.json").read_text(encoding="utf-8"))
    if config["format"] != "zlscan.ci-build-input/1":
        raise ValueError("unsupported CI backend format")
    archive = (source / config["bundle"]).resolve()
    if not archive.is_relative_to(source.resolve()) or sha(archive) != config["sha256"]:
        raise ValueError("CI backend bundle SHA256 differs")
    house = work / "backend/wheelhouse"
    house.mkdir(parents=True, exist_ok=False)
    manifest = work / "backend/manifest.json"
    prefix = config["prefix"]
    with tarfile.open(archive) as tar:
        for member in tar:
            parts = PurePosixPath(member.name).parts
            if parts == (prefix, "build_manifest.json") and member.isfile():
                manifest.write_bytes(tar.extractfile(member).read())
            elif len(parts) == 3 and parts[:2] == (prefix, "build_wheelhouse") and member.isfile():
                name = parts[-1]
                if name in (".", "..") or not name.endswith(".whl"):
                    raise ValueError("invalid backend wheel member")
                with tar.extractfile(member) as src, (house / name).open("xb") as dest:
                    shutil.copyfileobj(src, dest)
    validate_wheelhouse(house, manifest)
    return house, manifest


def wheel_manifest(house: Path, path: Path) -> None:
    value = {}
    for candidate in sorted(house.glob("*.whl")):
        with ZipFile(candidate) as archive:
            metadata = Parser().parsestr(
                archive.read(
                    next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                ).decode()
            )
        name = normalized(metadata["Name"])
        if name in value:
            raise ValueError("duplicate resolved runtime package")
        value[name] = dict(
            filename=candidate.name, version=metadata["Version"], sha256=sha(candidate)
        )
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    validate_wheelhouse(house, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--work", required=True, type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    work = args.work.resolve()
    if out.is_relative_to(source) or work.is_relative_to(source):
        raise ValueError("CI outputs/work must be outside checkout")
    out.mkdir(parents=True, exist_ok=False)
    work.mkdir(parents=True, exist_ok=False)
    runner = CICommands(out)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    summary = dict(
        format="zlscan.cross-platform-ci/1",
        status="running",
        source_commit=head,
        started_at=datetime.now(UTC).isoformat(),
        python=sys.version,
        platform=platform.platform(),
        machine=platform.machine(),
        github={
            key: os.environ.get(key)
            for key in (
                "GITHUB_ACTIONS",
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_SHA",
                "GITHUB_EVENT_NAME",
                "GITHUB_REF",
                "RUNNER_OS",
                "ImageOS",
                "ImageVersion",
            )
        },
        scope=(
            "actual configured runner; software tests and installation; "
            "no browser/hardware acceptance"
        ),
    )
    try:
        runner.run(
            "install_dev",
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--editable",
                str(source) + "[dev,legacy,ident]",
            ],
            cwd=source,
        )
        runner.run("dev_check", [sys.executable, "-m", "pip", "check"])
        runner.run("dev_packages", [sys.executable, "-m", "pip", "list", "--format=json"])
        runner.run("ruff", [sys.executable, "-m", "ruff", "check", "."], cwd=source)
        runner.run("format", [sys.executable, "-m", "ruff", "format", "--check", "."], cwd=source)
        runner.run(
            "pytest",
            [sys.executable, "-m", "pytest", "-q", "--junitxml=" + str(out / "pytest.xml")],
            cwd=source,
        )
        for tool in ("iverilog", "vvp"):
            path = shutil.which(tool)
            if path:
                runner.run(tool + "_version", [path, "-V"])
        backend, manifest = extract_backend(source, work)
        project = tomllib.loads((source / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        requirements = project["dependencies"] + project["optional-dependencies"]["ident"]
        house = work / "runtime-inputs"
        runner.run(
            "resolve_runtime",
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(house),
                *requirements,
            ],
        )
        runtime_manifest = work / "runtime-inputs.json"
        wheel_manifest(house, runtime_manifest)
        native = out / "wheel_audit"
        runner.run(
            "installed_wheel",
            [
                sys.executable,
                str(source / "tools/audit_wheel_install.py"),
                "--source",
                str(source),
                "--out",
                str(native),
                "--work",
                str(work / "wheel-work"),
                "--build-wheelhouse",
                str(backend),
                "--build-manifest",
                str(manifest),
                "--runtime-wheelhouse",
                str(house),
                "--runtime-manifest",
                str(runtime_manifest),
            ],
            cwd=source,
        )
        audit = json.loads((native / "audit.json").read_text(encoding="utf-8"))
        if (
            audit["source"]["dirty"]
            or audit["source"]["commit"] != head
            or not audit["committed_snapshot_verified"]
        ):
            raise ValueError("CI wheel source differs from committed checkout")
        examples = out / "installed_examples"
        for name in (
            "smoke",
            "static_cases",
            "dynamic_identification",
            "timing_identification",
            "segmented_rows",
        ):
            shutil.copytree(work / "wheel-work" / name, examples / name)
        shutil.copyfile(manifest, out / "build_manifest.json")
        shutil.copyfile(runtime_manifest, out / "runtime_input_manifest.json")
        summary["wheel"] = audit["wheel"]
        summary["repeated_build"] = audit["repeated_build"]
        summary["locked_runtime_distributions"] = audit["locked_distributions"]
        summary["status"] = "passed"
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        raise
    finally:
        summary["commands"] = runner.commands
        summary["finished_at"] = datetime.now(UTC).isoformat()
        (out / "ci-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
