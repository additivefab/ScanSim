"""Build a tracked-source wheel and prove a same-host isolated offline installation.

Network is used only to resolve/download wheels and isolated build dependencies. The
new runtime is installed from a complete SHA256-locked wheelhouse with --no-index.
This is not another physical machine, another OS, a browser test or hardware validation.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

ASSET_SUFFIXES = {".html", ".json", ".css", ".js", ".svg", ".png"}


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_wheelhouse(house: Path, manifest: Path, *, skip_project=False) -> dict:
    """Validate bytes and wheel metadata before any package installation."""
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ValueError("wheelhouse manifest must be a nonempty object")
    checked = {}
    for label, item in value.items():
        name = normalized(label)
        if name == "zlscan":
            if skip_project:
                continue
            raise ValueError("build manifest must not contain the project wheel")
        if not isinstance(item, dict) or name in checked:
            raise ValueError("invalid or repeated wheel metadata")
        filename = item.get("filename")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".whl")
            or not (house / filename).resolve().is_relative_to(house.resolve())
        ):
            raise ValueError("invalid wheel filename")
        path = house / filename
        if sha(path) != item.get("sha256"):
            raise ValueError(f"wheel SHA256 differs: {filename}")
        with ZipFile(path) as archive:
            meta = Parser().parsestr(
                archive.read(
                    next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                ).decode()
            )
        if normalized(meta["Name"]) != name or meta["Version"] != item.get("version"):
            raise ValueError(f"wheel metadata differs: {filename}")
        checked[name] = dict(item)
    if not checked:
        raise ValueError("wheelhouse has no external dependencies")
    return checked


def write_lock(path: Path, packages: dict, *, project_extra=False) -> None:
    path.write_text(
        "\n".join(
            f"{name}{'[ident]' if project_extra and name == 'zlscan' else ''}"
            f"=={item['version']} --hash=sha256:{item['sha256']}"
            for name, item in sorted(packages.items())
        )
        + "\n",
        encoding="utf-8",
    )


def verify_commit_snapshot(source: Path, snapshot: Path, commit: str) -> None:
    """Bind copied bytes to the fixed commit rather than a later working-tree observation."""
    algorithm = subprocess.check_output(
        ["git", "rev-parse", "--show-object-format"], cwd=source, text=True, encoding="utf-8"
    ).strip()
    tree = subprocess.check_output(["git", "ls-tree", "-r", "-z", commit], cwd=source)
    for entry in filter(None, tree.split(b"\0")):
        metadata, relative = entry.split(b"\t", 1)
        _, kind, expected = metadata.split()
        if kind != b"blob":
            raise ValueError("source snapshot does not support git submodules")
        path = snapshot / relative.decode()
        digest = hashlib.new(algorithm)
        digest.update(b"blob " + str(path.stat().st_size).encode() + b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        if digest.hexdigest() != expected.decode():
            raise ValueError(f"source snapshot differs from commit: {relative.decode()}")


def inspect_wheel(wheel: Path, expected: dict) -> dict:
    with ZipFile(wheel) as archive:
        entries = set(archive.namelist())
        for relative, digest in expected["assets"].items():
            assert relative in entries, f"wheel omits runtime asset: {relative}"
            assert hashlib.sha256(archive.read(relative)).hexdigest() == digest, relative
        for relative, digest in expected.get("python_sources", {}).items():
            assert relative in entries, f"wheel omits Python module: {relative}"
            assert hashlib.sha256(archive.read(relative)).hexdigest() == digest, relative
        meta_name = next(n for n in entries if n.endswith(".dist-info/METADATA"))
        meta = Parser().parsestr(archive.read(meta_name).decode())
        assert meta["Name"] == "zlscan" and meta["Version"] == expected["version"]
        assert not any(n.startswith(("tests/", "legacy/", ".git/")) for n in entries)
        info = archive.read(next(n for n in entries if n.endswith(".dist-info/WHEEL"))).decode()
        return dict(
            filename=wheel.name,
            sha256=sha(wheel),
            size_bytes=wheel.stat().st_size,
            metadata_version=meta["Version"],
            wheel_metadata=info,
            assets=expected["assets"],
            python_modules=len(expected.get("python_sources", {})),
        )


def audit(
    source: Path,
    output: Path,
    work: Path | None = None,
    *,
    build_wheelhouse: Path | None = None,
    build_manifest: Path | None = None,
    runtime_wheelhouse: Path | None = None,
    runtime_manifest: Path | None = None,
) -> dict:
    source = source.resolve()
    output = output.resolve()
    offline_args = [build_wheelhouse, build_manifest, runtime_wheelhouse, runtime_manifest]
    if any(x is not None for x in offline_args) and not all(x is not None for x in offline_args):
        raise ValueError("offline audit requires both build/runtime wheelhouses and manifests")
    offline = all(x is not None for x in offline_args)
    if offline:
        build_wheelhouse, build_manifest, runtime_wheelhouse, runtime_manifest = (
            p.resolve() for p in offline_args
        )
        build_inputs = validate_wheelhouse(build_wheelhouse, build_manifest)
        runtime_inputs = validate_wheelhouse(
            runtime_wheelhouse, runtime_manifest, skip_project=True
        )
    output.mkdir(parents=True, exist_ok=False)
    if work is None:
        cache = Path.home() / ".cache/zlscan-wheel-audit"
        cache.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="install-", dir=cache))
    else:
        work = work.resolve()
        work.mkdir(parents=True, exist_ok=False)
    assert not work.is_relative_to(source), "installation work directory must be outside checkout"
    env = dict(os.environ)
    for key in ["PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"]:
        env.pop(key, None)
    env.update(
        PYTHONNOUSERSITE="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    if offline:
        env["PIP_NO_INDEX"] = "1"
    commands = []
    (output / "work_dir.txt").write_text(str(work) + "\n", encoding="utf-8")

    def command(label: str, argv: list[str], cwd: Path = work) -> None:
        print(f"wheel audit: {label}", flush=True)
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8")
        (output / f"{label}.log").write_text(p.stdout + p.stderr, encoding="utf-8")
        commands.append(dict(label=label, argv=argv, returncode=p.returncode))
        (output / "progress.json").write_text(
            json.dumps(dict(work_dir=str(work), commands=commands), indent=2) + "\n",
            encoding="utf-8",
        )
        if p.returncode:
            raise RuntimeError(f"{label} failed ({p.returncode}); see {output / (label + '.log')}")

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8"
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=source, text=True, encoding="utf-8"
        ).strip()
    )
    epoch = subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", "HEAD"], cwd=source, text=True, encoding="utf-8"
    ).strip()
    env["SOURCE_DATE_EPOCH"] = epoch
    snapshot = work / "source-snapshot"
    snapshot.mkdir()
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=source).decode().split("\0")
    manifest = {}
    for name in filter(None, tracked):
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, destination)
        manifest[name] = sha(destination)
    (output / "source_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    if not dirty:
        verify_commit_snapshot(source, snapshot, commit)
    tree = ast.parse((snapshot / "src/zlscan/__init__.py").read_text(encoding="utf-8"))
    version = next(
        ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)
    )
    expected = dict(
        version=version,
        assets={
            p.relative_to(snapshot / "src").as_posix(): sha(p)
            for p in (snapshot / "src").rglob("*")
            if p.is_file() and p.suffix in ASSET_SUFFIXES
        },
        python_sources={
            p.relative_to(snapshot / "src").as_posix(): sha(p)
            for p in (snapshot / "src").rglob("*.py")
            if p.is_file()
        },
    )
    (output / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    builder = sys.executable
    if offline:
        write_lock(output / "build.lock", build_inputs)
        write_lock(output / "external-runtime.lock", runtime_inputs)
        command("build_venv", [sys.executable, "-m", "venv", str(work / "builder")])
        builder = str(
            work / "builder" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        command(
            "build_install",
            [
                builder,
                "-m",
                "pip",
                "install",
                "--quiet",
                "--no-index",
                "--find-links",
                str(build_wheelhouse),
                "--require-hashes",
                "-r",
                str(output / "build.lock"),
            ],
        )
        command("build_check", [builder, "-m", "pip", "check"])
        command("build_list", [builder, "-m", "pip", "list", "--format=json"])
    command(
        "build",
        [
            builder,
            "-m",
            "pip",
            "wheel",
            "--quiet",
            "--no-deps",
            *(["--no-index", "--no-build-isolation"] if offline else []),
            "--wheel-dir",
            str(work / "dist"),
            str(snapshot),
        ],
    )
    wheel = next((work / "dist").glob("*.whl"))
    wheel_info = inspect_wheel(wheel, expected)
    repeated = None
    if offline:
        second = work / "source-repeat"
        for relative, digest in manifest.items():
            assert sha(snapshot / relative) == digest, relative
            target = second / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(snapshot / relative, target)
        command(
            "build_repeat",
            [
                builder,
                "-m",
                "pip",
                "wheel",
                "--quiet",
                "--no-deps",
                "--no-index",
                "--no-build-isolation",
                "--wheel-dir",
                str(work / "dist-repeat"),
                str(second),
            ],
        )
        repeat_wheel = next((work / "dist-repeat").glob("*.whl"))
        assert sha(repeat_wheel) == sha(wheel), "repeated wheel differs byte for byte"
        for relative, digest in manifest.items():
            assert sha(second / relative) == sha(snapshot / relative) == digest, relative
        repeated = dict(sha256=sha(repeat_wheel), byte_identical=True, independent_source_copy=True)
    house = work / "wheelhouse"
    command(
        "download",
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--quiet",
            "--only-binary=:all:",
            "--dest",
            str(house),
            *(
                [
                    "--no-index",
                    "--find-links",
                    str(runtime_wheelhouse),
                    "--require-hashes",
                    "-r",
                    str(output / "external-runtime.lock"),
                ]
                if offline
                else [str(wheel) + "[ident]"]
            ),
        ],
    )
    if offline:
        shutil.copyfile(wheel, house / wheel.name)
    locked = {}
    for candidate in sorted(house.glob("*.whl")):
        with ZipFile(candidate) as archive:
            metadata = Parser().parsestr(
                archive.read(
                    next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
                ).decode()
            )
        name = normalized(metadata["Name"])
        assert name not in locked, f"multiple candidate wheels for {name}"
        locked[name] = dict(
            version=metadata["Version"], filename=candidate.name, sha256=sha(candidate)
        )
    lines = []
    for name, item in sorted(locked.items()):
        extras = "[ident]" if name == "zlscan" else ""
        lines.append(f"{name}{extras}=={item['version']} --hash=sha256:{item['sha256']}")
    lock = output / "requirements.lock"
    lock.write_text(
        "# Runtime + ident only, for the accompanying wheel-install audit's exact target.\n"
        + "\n".join(lines)
        + "\n",
        encoding="utf-8",
    )
    (output / "wheelhouse_manifest.json").write_text(
        json.dumps(locked, indent=2) + "\n", encoding="utf-8"
    )
    prefix = work / "runtime"
    command("venv", [sys.executable, "-m", "venv", str(prefix)])
    python = prefix / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    command(
        "install",
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--no-index",
            "--find-links",
            str(house),
            "--require-hashes",
            "-r",
            str(lock),
        ],
    )
    command("pip_check", [str(python), "-m", "pip", "check"])
    command("pip_list", [str(python), "-m", "pip", "list", "--format=json"])
    shutil.copyfile(snapshot / "tools/wheel_smoke.py", work / "wheel_smoke.py")
    shutil.copyfile(snapshot / "tools/audit_static_identification.py", work / "static_audit.py")
    command(
        "smoke",
        [
            str(python),
            "-I",
            str(work / "wheel_smoke.py"),
            "--out",
            str(work / "smoke"),
            "--expected",
            str(output / "expected.json"),
            "--prefix",
            str(prefix),
            "--forbidden-source",
            str(source / "src"),
        ],
    )
    command(
        "static_cases",
        [str(python), "-I", str(work / "static_audit.py"), "--out", str(work / "static_cases")],
    )
    command(
        "static_verify",
        [str(python), "-I", str(work / "static_audit.py"), "--verify", str(work / "static_cases")],
    )
    for feature in (
        "dynamic_identification",
        "timing_identification",
        "segmented_rows",
        "epoch_calibrations",
    ):
        driver = work / (feature + "_audit.py")
        shutil.copyfile(snapshot / "tools" / ("audit_" + feature + ".py"), driver)
        target = work / feature
        command(feature, [str(python), "-I", str(driver), "--out", str(target)])
        command(
            feature + "_verify", [str(python), "-I", str(driver), "--out", str(target), "--verify"]
        )
        shutil.copyfile(target / "audit.json", output / (feature + ".json"))
    for relative, digest in manifest.items():
        assert sha(snapshot / relative) == digest, relative
    smoke = json.loads((work / "smoke/smoke.json").read_text(encoding="utf-8"))
    summary = dict(
        format="zlscan.wheel-install-audit/1",
        source=dict(commit=commit, dirty=dirty),
        committed_snapshot_verified=not dirty,
        dependency_resolution="offline_hash_locked_inputs" if offline else "online",
        repeated_build=repeated,
        build_inputs_sha256=sha(build_manifest) if offline else None,
        runtime_inputs_sha256=sha(runtime_manifest) if offline else None,
        work_dir=str(work),
        wheel=wheel_info,
        locked_distributions=len(locked),
        runtime_target={
            k: smoke[k] for k in ["python", "platform", "process_arch", "prefix", "module_path"]
        },
        http=smoke["http"],
        hardware_check=smoke["hardware_check"],
        commands=commands,
        scope="same-host isolated runtime+ident; no browser, other-OS or hardware acceptance",
    )
    shutil.copyfile(wheel, output / wheel.name)
    shutil.copyfile(work / "smoke/smoke.json", output / "smoke.json")
    shutil.copyfile(work / "static_cases/summary.json", output / "static_cases.json")
    (output / "audit.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            dict(
                version=version,
                assets=len(expected["assets"]),
                locked_distributions=len(locked),
                status="passed",
                work_dir=str(work),
            )
        ),
        flush=True,
    )
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--work", type=Path)
    p.add_argument("--build-wheelhouse", type=Path)
    p.add_argument("--build-manifest", type=Path)
    p.add_argument("--runtime-wheelhouse", type=Path)
    p.add_argument("--runtime-manifest", type=Path)
    args = p.parse_args()
    audit(
        args.source,
        args.out,
        args.work,
        build_wheelhouse=args.build_wheelhouse,
        build_manifest=args.build_manifest,
        runtime_wheelhouse=args.runtime_wheelhouse,
        runtime_manifest=args.runtime_manifest,
    )


if __name__ == "__main__":
    main()
