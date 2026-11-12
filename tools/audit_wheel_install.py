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


def inspect_wheel(wheel: Path, expected: dict) -> dict:
    with ZipFile(wheel) as archive:
        entries = set(archive.namelist())
        for relative, digest in expected["assets"].items():
            assert relative in entries, f"wheel omits runtime asset: {relative}"
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
        )


def audit(source: Path, output: Path, work: Path | None = None) -> dict:
    source = source.resolve()
    output = output.resolve()
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
    env.update(PYTHONNOUSERSITE="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    commands = []
    (output / "work_dir.txt").write_text(str(work) + "\n")

    def command(label: str, argv: list[str], cwd: Path = work) -> None:
        print(f"wheel audit: {label}", flush=True)
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
        (output / f"{label}.log").write_text(p.stdout + p.stderr)
        commands.append(dict(label=label, argv=argv, returncode=p.returncode))
        (output / "progress.json").write_text(
            json.dumps(dict(work_dir=str(work), commands=commands), indent=2) + "\n"
        )
        if p.returncode:
            raise RuntimeError(f"{label} failed ({p.returncode}); see {output / (label + '.log')}")

    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    dirty = bool(
        subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True).strip()
    )
    epoch = subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", "HEAD"], cwd=source, text=True
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
    (output / "source_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    tree = ast.parse((snapshot / "src/zlscan/__init__.py").read_text())
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
    )
    (output / "expected.json").write_text(json.dumps(expected, indent=2) + "\n")
    command(
        "build",
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--quiet",
            "--no-deps",
            "--wheel-dir",
            str(work / "dist"),
            str(snapshot),
        ],
    )
    wheel = next((work / "dist").glob("*.whl"))
    wheel_info = inspect_wheel(wheel, expected)
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
            str(wheel) + "[ident]",
        ],
    )
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
        + "\n"
    )
    (output / "wheelhouse_manifest.json").write_text(json.dumps(locked, indent=2) + "\n")
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
    shutil.copyfile(source / "tools/wheel_smoke.py", work / "wheel_smoke.py")
    shutil.copyfile(source / "tools/audit_static_identification.py", work / "static_audit.py")
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
    smoke = json.loads((work / "smoke/smoke.json").read_text())
    summary = dict(
        format="zlscan.wheel-install-audit/1",
        source=dict(commit=commit, dirty=dirty),
        work_dir=str(work),
        wheel=wheel_info,
        locked_distributions=len(locked),
        runtime_target={
            k: smoke[k] for k in ["python", "platform", "process_arch", "prefix", "module_path"]
        },
        http=smoke["http"],
        hardware_check=smoke["hardware_check"],
        commands=commands,
        scope="same-host isolated runtime+ident smoke; no browser, other-OS or hardware acceptance",
    )
    shutil.copyfile(wheel, output / wheel.name)
    shutil.copyfile(work / "smoke/smoke.json", output / "smoke.json")
    shutil.copyfile(work / "static_cases/summary.json", output / "static_cases.json")
    (output / "audit.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
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
    args = p.parse_args()
    audit(args.source, args.out, args.work)


if __name__ == "__main__":
    main()
