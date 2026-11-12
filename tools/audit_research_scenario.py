"""Audit public scenario CLI, self-contained rerun, automatic replay and refusal paths."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from zlscan.engine.research_config import load_scenario
from zlscan.engine.saved_service import saved_service
from zlscan.io.manifest import sha256_file


def hashes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in root.rglob("*") if p.is_file()}


def verify(root: Path) -> dict:
    expected = json.loads((root / "source_manifest.json").read_text())
    assert hashes(root / "physical") == expected
    run = json.loads((root / "physical/run.json").read_text())
    requested = load_scenario(root / "scenario.json")
    assert requested.primary.hash() == run["config_hash"]
    provenance = run["scenario_configuration"]
    assert provenance["primary_hash"] == run["config_hash"]
    assert provenance["input_sha256"] == sha256_file(root / "scenario.json")
    assert (root / "physical/inputs/scenario.json").read_bytes() == requested.original
    budget = json.loads((root / "budget/budget.json").read_text())
    assert budget["scenario_configuration"]["primary_hash"] == run["config_hash"]
    assert budget["flagged_assumed_scenario"]
    assert any(c["status"] == "UNRESOLVED" for c in budget["conclusions"])
    assert not any(c["status"] == "FAIL" for c in budget["conclusions"])
    source = np.load(root / "physical/truth/source_codes.npy", allow_pickle=False)
    repeated = np.load(root / "repeat/truth/source_codes.npy", allow_pickle=False)
    assert np.array_equal(source, repeated)
    with np.load(root / "replayed/samples.npz", allow_pickle=False) as r:
        assert np.array_equal(r["codes"], source.reshape(-1)[2750:2775])
    with saved_service(root / "physical") as service:
        saved = service.runs["saved"]
        assert saved.cfg.hash() == run["config_hash"]
        trace = service.traces(saved, "adc.code", 0, saved.steps, 40, "instrument")
        assert trace["missing_count"] > 0 and len(trace["values"]) <= 40
    negative = json.loads((root / "over_limit_budget/budget.json").read_text())
    assert any(
        c["id"] == "photons.anode.D0" and c["status"] == "FAIL" for c in negative["conclusions"]
    )
    assert not (root / "bad_output").exists()
    assert not (root / "hardware_budget").exists()
    assert hashes(root / "physical") == expected
    result = dict(
        config_hash=run["config_hash"],
        input_sha256=provenance["input_sha256"],
        source_file_count=len(expected),
        source_samples=int(source.size),
        repeat_codes_equal=True,
        replay_codes_equal=True,
        source_unchanged=True,
        observed_missing_count=trace["missing_count"],
        observed_bins=len(trace["values"]),
        budget_unresolved=True,
        over_limit_preserved=True,
        origin="SIMULATED",
        scope="finite scenario CLI and saved services; no browser/hardware acceptance",
    )
    print("Scenario budget/run/archive/replay/refusal checks passed")
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--out", type=Path)
    g.add_argument("--verify", type=Path)
    args = p.parse_args()
    if args.out:
        root = args.out.resolve()
        root.mkdir(parents=True, exist_ok=False)
        fixture = Path(__file__).resolve().parents[1] / "configs/research_scenario.json"
        (root / "scenario.json").write_bytes(fixture.read_bytes())
        commands = []

        def run(label, argv, expected=0):
            command = [sys.executable, "-m", "zlscan", *argv]
            r = subprocess.run(command, capture_output=True, text=True)
            (root / f"{label}.log").write_text(r.stdout + r.stderr)
            commands.append(
                dict(label=label, argv=command, expected=expected, returncode=r.returncode)
            )
            (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n")
            assert r.returncode == expected, (label, r.stdout, r.stderr)

        run(
            "budget",
            ["budget", "--config", str(root / "scenario.json"), "--out", str(root / "budget")],
            4,
        )
        run(
            "physical",
            [
                "run",
                "--mode",
                "physical",
                "--config",
                str(root / "scenario.json"),
                "--out",
                str(root / "physical"),
            ],
        )
        (root / "source_manifest.json").write_text(
            json.dumps(hashes(root / "physical"), indent=2) + "\n"
        )
        run(
            "repeat",
            [
                "run",
                "--mode",
                "physical",
                "--config",
                str(root / "physical/inputs/resolved_scenario.json"),
                "--out",
                str(root / "repeat"),
            ],
        )
        run(
            "replay",
            [
                "replay",
                "--run",
                str(root / "physical"),
                "--start-step",
                "11000",
                "--steps",
                "100",
                "--out",
                str(root / "replayed"),
            ],
        )
        negative = json.loads(fixture.read_text())
        negative["chain"]["source"]["power_w"] = 0.00073
        (root / "over_limit.json").write_text(json.dumps(negative))
        run(
            "over_limit",
            [
                "budget",
                "--config",
                str(root / "over_limit.json"),
                "--out",
                str(root / "over_limit_budget"),
            ],
            1,
        )
        bad = json.loads(fixture.read_text())
        bad["chain"]["adc"] = {"bits": 17}
        (root / "bad.json").write_text(json.dumps(bad))
        run(
            "bad",
            [
                "run",
                "--mode",
                "physical",
                "--config",
                str(root / "bad.json"),
                "--out",
                str(root / "bad_output"),
            ],
            1,
        )
        run(
            "hardware",
            [
                "budget",
                "--config",
                str(root / "scenario.json"),
                "--mode",
                "hardware_constrained",
                "--out",
                str(root / "hardware_budget"),
            ],
            1,
        )
        (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n")
    else:
        root = args.verify.resolve()
        assert verify(root) == json.loads((root / "audit.json").read_text())
        print("Fresh-process scenario checks match exactly")


if __name__ == "__main__":
    main()
