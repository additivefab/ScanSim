"""Compare frozen probe responses and counterbalanced old/new service query timings."""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
import pstats
import shutil
import subprocess
import sys
import tarfile
import time
import types
from pathlib import Path
from statistics import median

from zlscan.engine.saved_service import saved_service
from zlscan.evidence.records import environment_info, git_state
from zlscan.io.manifest import sha256_file

BASELINE = "4f9c946845008776eb92bafbab93336b1f52558e"
MODULE = "src/zlscan/io/probe_store.py"
CASES = [
    ("overview", 0, 11000, 20),
    ("fine", 10200, 11000, 800),
    ("partial", 123, 10031, 17),
    ("left", -20, 333, 5),
    ("right", 10333, 12000, 13),
    ("single", 10321, 10322, 40),
    ("boundary", 511, 1035, 7),
    ("wide", 0, 11000, 4096),
]


def _git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def unpack(case: Path, target: Path):
    manifest = json.loads((case / "source_manifest.json").read_text())
    with tarfile.open(case / "source.tar.gz", "r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            relative = Path(member.name)
            if not member.isfile() or relative.parts[0] != "source" or ".." in relative.parts:
                raise ValueError("source archive contains unsupported entries")
            name = Path(*relative.parts[1:]).as_posix()
            if name not in manifest:
                raise ValueError("unmanifested source file")
            path = target / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, path.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            assert sha256_file(path) == manifest[name], name
    assert hashes(target / "source") == manifest
    return target / "source"


def hashes(source):
    return {
        p.relative_to(source).as_posix(): sha256_file(p) for p in source.rglob("*") if p.is_file()
    }


def run(output: Path, inputs: Path, baseline: str, repeats: int):
    output.mkdir(parents=True, exist_ok=False)
    code = git_state(Path.cwd())
    baseline = _git("rev-parse", "--verify", baseline + "^{commit}")
    changed = set(_git("diff", "--name-only", baseline, "--", "src/zlscan").splitlines())
    changed |= set(_git("diff", "--name-only", "--", "src/zlscan").splitlines())
    assert changed <= {MODULE}, changed
    blob = subprocess.check_output(["git", "show", baseline + ":" + MODULE])
    (output / "baseline_probe_store.py").write_bytes(blob)
    old = types.ModuleType("zlscan_probe_query_baseline")
    sys.modules[old.__name__] = old
    exec(compile(blob, baseline + ":" + MODULE, "exec"), old.__dict__)
    report = dict(
        code=code,
        baseline_commit=baseline,
        baseline_sha256=hashlib.sha256(blob).hexdigest(),
        current_sha256=sha256_file(Path(MODULE)),
        environment=environment_info(),
        scope=(
            "same service and frozen source, only probe reader swapped; query excludes "
            "registration, HTTP/JSON/network/browser; OS caches/background load uncontrolled; "
            "no response cache"
        ),
        repeats=repeats,
        policies={},
    )
    responses = {}
    archive_summary = json.loads((inputs / "summary.json").read_text())
    for policy in ["raw", "summary"]:
        case = inputs / policy
        assert sha256_file(case / "source.tar.gz") == archive_summary[policy]["archive_sha256"]
        source = unpack(case, output / policy)
        before = hashes(source)
        trace_state = json.loads((source / "checkpoint.json").read_bytes())["trace"]
        timings = {
            name: {kind: [] for kind in ["baseline", "current"]} for name in ["overview", "fine"]
        }
        registration = {kind: [] for kind in ["baseline", "current"]}
        with saved_service(source) as service:
            current = service.probes
            with old.TraceReader(
                source / "probes", json.loads((source / "checkpoint.json").read_bytes())["trace"]
            ) as previous:
                for name, start, end, width in CASES:
                    for channel in [0, 1]:
                        answers = []
                        for reader in [previous, current]:
                            service.probes = reader
                            answer = service.traces(
                                service.runs[f"recording-c{channel}"],
                                "pmt.power_w",
                                start,
                                end,
                                width,
                                "research",
                            )
                            answers.append(answer)
                        assert json.dumps(answers[0], sort_keys=True) == json.dumps(
                            answers[1], sort_keys=True
                        ), (policy, name, channel)
                        responses[f"{policy}/{name}/c{channel}"] = answers[1]
                for name, start, end, width in CASES[:2]:
                    for repeat in range(repeats):
                        order = [("baseline", previous), ("current", current)]
                        if repeat % 2:
                            order.reverse()
                        for kind, reader in order:
                            service.probes = reader
                            clock = time.perf_counter()
                            result = service.traces(
                                service.runs["recording-c0"],
                                "pmt.power_w",
                                start,
                                end,
                                width,
                                "research",
                            )
                            timings[name][kind].append(1000 * (time.perf_counter() - clock))
                            assert result == responses[f"{policy}/{name}/c0"]
                for kind, reader in [("baseline", previous), ("current", current)]:
                    service.probes = reader
                    profile = cProfile.Profile()
                    profile.runcall(
                        service.traces,
                        service.runs["recording-c0"],
                        "pmt.power_w",
                        0,
                        11000,
                        20,
                        "research",
                    )
                    stream = io.StringIO()
                    pstats.Stats(profile, stream=stream).sort_stats("cumtime").print_stats(30)
                    (output / f"profile_{policy}_{kind}.txt").write_text(stream.getvalue())
                for repeat in range(3):
                    constructors = [("baseline", type(previous)), ("current", type(current))]
                    if repeat % 2:
                        constructors.reverse()
                    for kind, constructor in constructors:
                        clock = time.perf_counter()
                        with constructor(source / "probes", trace_state):
                            registration[kind].append(1000 * (time.perf_counter() - clock))
            service.probes = current
        assert hashes(source) == before
        report["policies"][policy] = dict(
            source_files=len(before),
            source_unchanged=True,
            reader_registration={
                kind: dict(milliseconds=values, median_ms=median(values))
                for kind, values in registration.items()
            },
            timings={
                name: {
                    kind: dict(milliseconds=values, median_ms=median(values))
                    for kind, values in kinds.items()
                }
                for name, kinds in timings.items()
            },
        )
    report["identical_responses"] = len(responses)
    (output / "responses.json").write_text(json.dumps(responses, indent=2, sort_keys=True) + "\n")
    (output / "audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--inputs", type=Path, default=Path("evidence_runs/2026-11-13_probe_recording/record")
    )
    p.add_argument("--baseline", default=BASELINE)
    p.add_argument("--repeats", type=int, default=20)
    p.add_argument("--compare", type=Path, help="fresh-process comparison of every saved response")
    args = p.parse_args()
    if not 1 <= args.repeats <= 100:
        p.error("repeats must be 1..100")
    run(args.out.resolve(), args.inputs.resolve(), args.baseline, args.repeats)
    if args.compare:
        assert json.loads((args.compare / "responses.json").read_text()) == json.loads(
            (args.out / "responses.json").read_text()
        )
        print("Fresh-process responses agree exactly")


if __name__ == "__main__":
    main()
