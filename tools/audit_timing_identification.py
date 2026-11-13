"""Synthetic counter/reference exports through public timing CLI and fresh-process recomputation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from zlscan.ident.timing import TimingFitConfig, identify_timing_files
from zlscan.io.manifest import sha256_file

MODES = ("nominal", "missing", "no_phase", "cycle_only", "mismatch", "reuse")
PERIODS = (1000000, 1100000, 900000)


def make_case(root, mode):
    root.mkdir()
    rng = np.random.default_rng(20261113)
    captures = []
    for k in range(3):
        ids = (
            list(range(0, 181, 3))
            if mode == "cycle_only"
            else [n for n in range(181) if mode != "missing" or n % 11 != 5]
        )
        edges = []
        export = []
        origin = 2**60 + k * 10**10
        for n in ids:
            q, r = divmod(n, 3)
            tick = origin + q * sum(PERIODS) + sum(PERIODS[:r]) + int(np.rint(rng.normal(0, 20)))
            if mode == "mismatch" and k == 2 and n == ids[-1]:
                tick += 1000
            sync = dict(
                kind="line_sync",
                origin="SIMULATED",
                visibility="OBSERVABLE",
                derivation="SENSOR",
                clock_domain=7,
                count=n,
                tick=tick,
                session_id=k,
                epoch=0,
                capability_ref="synthetic/absolute-sync-counter",
            )
            anchor = (
                dict(
                    sync,
                    kind="facet_index",
                    capability_ref="synthetic/revolution-reference",
                    payload=dict(phase=n % 3),
                )
                if n == ids[0] and mode != "no_phase"
                else None
            )
            edges.append(dict(line_index=str(n), sync=sync, anchor=anchor))
            export.append(
                dict(
                    observed_counter=str(n),
                    timestamp_ps=str(tick),
                    reference_phase=anchor["payload"]["phase"] if anchor else None,
                )
            )
        raw = root / f"raw-{k}.json"
        raw.write_text(
            json.dumps(
                dict(
                    origin="synthetic",
                    session=k,
                    seed=20261113,
                    observed_reference_channel=mode != "no_phase",
                    rows=export,
                ),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        frame = dict(
            format="zlscan.sync-observations/1",
            source_kind="synthetic",
            device="synthetic timestamp instrument",
            measurement_point="counter and optional reference channel",
            settings_source=(
                "explicit analytic fixture; 1 ps quantization; 20 ps Gaussian input sigma"
            ),
            extraction_method=(
                "tagged export with explicit absolute counter; no phase guessed from timing"
            ),
            raw_source=raw.name,
            raw_sha256=sha256_file(raw),
            session_id=k,
            epoch=0,
            clock_domain=7,
            counter_capability_ref="synthetic/absolute-sync-counter",
            phase_capability_ref="synthetic/revolution-reference" if mode != "no_phase" else None,
            edges=edges,
        )
        path = root / f"edges-{k}.json"
        path.write_text(json.dumps(frame, indent=2) + "\n", encoding="utf-8")
        captures.append(
            dict(
                name=f"acquisition-{k}",
                path=path.name,
                sha256=sha256_file(path),
                partition="fit" if k < 2 else "validation",
                standard_uncertainty_ps=20.0,
                uncertainty_source=(
                    "synthetic Gaussian input sigma; timestamps rounded to ps; conditional model"
                ),
            )
        )
    if mode == "reuse":
        captures[-1].update(path=captures[0]["path"], sha256=captures[0]["sha256"])
    cfg = dict(
        format="zlscan.timing-fit-config/1",
        facets=3,
        confidence_level=0.95,
        rank_rtol=1e-10,
        assumption_source=(
            "synthetic counter/reference and conditional timestamp errors; not actual hardware"
        ),
        error_model="independent_gaussian_known_timestamp_sigma",
        validation_max_abs_residual_ps=200.0,
        captures=captures,
    )
    (root / "config.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def verify(root):
    results = {}
    for mode in MODES:
        folder = root / mode
        config = folder / "config.json"
        r = identify_timing_files(TimingFitConfig.model_validate_json(config.read_bytes()), folder)
        r["config_sha256"] = sha256_file(config)
        assert r == json.loads((folder / "result/run.json").read_text(encoding="utf-8")), mode
        assert not r["parameter_upgrades"] and not r["hard_acceptance_eligible"]
        results[mode] = r
    nominal = results["nominal"]
    assert nominal["validation"]["status"] == "compatible"
    estimates = [
        nominal["facet_period_fit"]["parameters"][f"period_{i}_ps"]["value"] for i in range(3)
    ]
    error = max(abs(a - b) for a, b in zip(estimates, PERIODS, strict=True))
    assert error < 100
    assert (
        results["missing"]["status"] == "identified"
        and int(results["missing"]["captures"][0]["missing_line_counts"]) > 0
    )
    assert results["no_phase"]["facet_period_fit"]["status"] == "unidentifiable"
    assert results["cycle_only"]["facet_period_fit"]["cycle_period"]["value"] is not None
    assert all(
        p["value"] is None for p in results["cycle_only"]["facet_period_fit"]["parameters"].values()
    )
    assert results["mismatch"]["facet_period_fit"] == nominal["facet_period_fit"]
    assert results["mismatch"]["validation"]["status"] == "mismatch"
    assert results["reuse"]["status"] == "rejected"
    manifest = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    for rel, digest in manifest.items():
        assert sha256_file(root / rel) == digest, rel
    return dict(
        source_kind="synthetic",
        seed=20261113,
        cli_cases=6,
        source_files_unchanged=len(manifest),
        facet_periods_ps=estimates,
        max_facet_period_error_ps=error,
        criterion_error_ps=100,
        heldout_max_abs_residual_ps=nominal["validation"]["max_abs_residual_ps"],
        criterion_heldout_ps=200,
        original_tick_ps=nominal["captures"][0]["origin_tick_ps"],
        real_measurements_obtained=False,
        parameter_upgrades=[],
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--verify", action="store_true")
    args = p.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert verify(root) == json.loads((root / "audit.json").read_text(encoding="utf-8"))
        print("six timing CLI results recomputed exactly; original sources unchanged")
        return
    root.mkdir(parents=True, exist_ok=False)
    for mode in MODES:
        make_case(root / mode, mode)
    manifest = {
        str(p.relative_to(root)): sha256_file(p) for p in sorted(root.rglob("*")) if p.is_file()
    }
    (root / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    commands = []
    for mode in MODES:
        argv = [
            sys.executable,
            "-m",
            "zlscan",
            "ident",
            "--timing-fit",
            str(root / mode / "config.json"),
            "--out",
            str(root / mode / "result"),
        ]
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        (root / (mode + ".stdout")).write_text(r.stdout, encoding="utf-8")
        (root / (mode + ".stderr")).write_text(r.stderr, encoding="utf-8")
        expected = 0 if mode in ("nominal", "missing") else 1
        commands.append(dict(argv=argv, returncode=r.returncode, expected_returncode=expected))
        (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")
        assert r.returncode == expected, r.stdout + r.stderr
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n", encoding="utf-8")
    print("observable timing, missing edges, phase/rank/validation/reuse gates passed")


if __name__ == "__main__":
    main()
