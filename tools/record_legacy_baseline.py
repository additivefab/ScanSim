"""Rerun the frozen legacy packages and write evidence records (task S0-T03).

One record per package under evidence_runs/<date>_legacy_package_{a,b}/. The expected values
are the metrics the authors delivered inside legacy/ (status author_reported); a rerun that
reproduces them is recorded as an independent_rerun.

The working tree must be clean, so that the recorded commit really is the code that ran.
Large outputs stay in a scratch directory and are not kept.
"""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from zlscan.evidence import legacy_runner as lr
from zlscan.evidence.records import compare, environment_info, git_state, write_record
from zlscan.io.manifest import sha256_file

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evidence_runs"


def _hashes(relative_paths: list[str]) -> dict[str, str]:
    return {p: sha256_file(ROOT / p) for p in relative_paths}


def _log(results: list[tuple[str, lr.CommandResult]]) -> str:
    parts = []
    for title, result in results:
        parts.append(f"===== {title}\n$ {' '.join(result.argv)}\n(cwd {result.cwd})\n")
        parts.append(f"{result.output}\n[exit {result.returncode}, {result.seconds:.1f} s]\n\n")
    return "".join(parts)


def _conclusion(criteria: list[dict], results: list[tuple[str, lr.CommandResult]]) -> str:
    ok = all(c["passed"] for c in criteria) and all(r.returncode == 0 for _, r in results)
    return "pass" if ok else "fail"


def record_package_a(scratch: Path, code: dict, date: str) -> tuple[Path, str]:
    started = datetime.now(UTC)
    package = lr.stage_package("package_a", scratch)
    demo, metrics = lr.run_a_demo(package, scratch / "out_a")
    own = lr.run_a_own_tests(package)
    results = [("package A demo", demo), ("package A own tests", own)]
    delivered = lr.delivered_metrics(lr.A_DELIVERED_METRICS)
    criteria = [
        compare(f"demo.{k}", delivered[k], metrics.get(k), rel_tol=lr.FLOAT_REL_TOL)
        for k in lr.A_KEYS
    ]
    criteria.append(compare("own_tests.passed", lr.A_OWN_TESTS, lr.parse_pytest_passed(own.output)))
    conclusion = _conclusion(criteria, results)
    delivered_env = delivered.get("environment")
    path = write_record(
        RUNS / f"{date}_legacy_package_a",
        name="legacy_package_a",
        status="independent_rerun",
        conclusion=conclusion,
        commands=[r.as_command() for _, r in results],
        inputs=_hashes(
            [
                "legacy/MANIFEST.json",
                "legacy/package_a/run_demo.py",
                "legacy/package_a/config/demo.json",
                "legacy/package_a/src/scanner_sim.py",
                f"legacy/{lr.A_DELIVERED_METRICS}",
            ]
        ),
        seed=lr.read_json(lr.LEGACY_ROOT / "package_a/config/demo.json")["seed"],
        criteria=criteria,
        started_at=started,
        finished_at=datetime.now(UTC),
        log_text=_log(results),
        code=code,
        environment=environment_info(),
        notes=(
            "Expected values are the metrics delivered by the package author inside legacy/ "
            f"(author_reported; produced in {delivered_env}). Floats are compared with relative "
            f"tolerance {lr.FLOAT_REL_TOL}, integers exactly. "
            "Ran in a scratch copy; legacy/ untouched."
        ),
    )
    return path, conclusion


def record_package_b(scratch: Path, code: dict, date: str) -> tuple[Path, str]:
    started = datetime.now(UTC)
    package = lr.stage_package("package_b", scratch)
    results: list[tuple[str, lr.CommandResult]] = []
    criteria: list[dict] = []
    for config in lr.B_CONFIGS:
        result, metrics = lr.run_b_sim(package, config, scratch / f"out_b_{config}")
        results.append((f"package B {config}", result))
        delivered = lr.delivered_metrics(lr.B_DELIVERED[config])
        criteria += [
            compare(f"{config}.{k}", delivered[k], metrics.get(k), rel_tol=lr.FLOAT_REL_TOL)
            for k in lr.B_KEYS
        ]
    own = lr.run_b_own_tests(package)
    results.append(("package B own tests", own))
    criteria.append(compare("own_tests.ran", lr.B_OWN_TESTS, lr.parse_unittest_ran(own.output)))
    seeds = {
        c: lr.read_json(lr.LEGACY_ROOT / f"package_b/configs/{c}.json")["seed"]
        for c in lr.B_CONFIGS
    }
    inputs = _hashes(
        [
            "legacy/MANIFEST.json",
            "legacy/package_b/run_sim.py",
            "legacy/package_b/src/scanner.py",
            *[f"legacy/package_b/configs/{c}.json" for c in lr.B_CONFIGS],
            *[f"legacy/{lr.B_DELIVERED[c]}" for c in lr.B_CONFIGS],
        ]
    )
    conclusion = _conclusion(criteria, results)
    path = write_record(
        RUNS / f"{date}_legacy_package_b",
        name="legacy_package_b",
        status="independent_rerun",
        conclusion=conclusion,
        commands=[r.as_command() for _, r in results],
        inputs=inputs,
        seed=seeds,
        criteria=criteria,
        started_at=started,
        finished_at=datetime.now(UTC),
        log_text=_log(results),
        code=code,
        environment=environment_info(),
        notes=(
            "Expected values are the metrics delivered by the package author inside legacy/ "
            "(author_reported). Floats are compared with relative tolerance "
            f"{lr.FLOAT_REL_TOL}, integers exactly. Ran in a scratch copy; legacy/ untouched."
        ),
    )
    return path, conclusion


def main() -> int:
    code = git_state(ROOT)
    if code["dirty"] is not False:
        print(
            "refusing to record: the working tree is not clean (or git is unavailable), "
            "so the commit would not describe the code that ran. Commit first.",
            file=sys.stderr,
        )
        return 2
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    exit_code = 0
    with tempfile.TemporaryDirectory(prefix="zlscan_legacy_") as tmp:
        for record in (record_package_a, record_package_b):
            path, conclusion = record(Path(tmp), code, date)
            print(f"{conclusion}: {path.relative_to(ROOT)}")
            exit_code |= conclusion != "pass"
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
