"""Analytic noisy scope files through public dynamic CLI, plus fresh-process recomputation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from zlscan.ident.dynamic import DynamicFitConfig, identify_dynamic_files
from zlscan.io.manifest import sha256_file


def sources(root):
    rng = np.random.default_rng(20261113)
    experiments = []
    for i in range(8):
        direction = "rise" if i % 2 == 0 else "fall"
        partition = "fit" if i < 4 else "validation"
        tau = 0.002 if direction == "rise" else 0.005
        amplitude = 1.5 + 0.2 * (i // 2) if direction == "rise" else -1.2 - 0.1 * (i // 2)
        baseline = 0.3 + 0.05 * i
        step = i * 0.1
        t = np.linspace(-0.003, 0.04, 173) + step
        y = (
            baseline
            + amplitude * (-np.expm1(-np.maximum(t - step, 0) / tau))
            + rng.normal(0, 0.001, len(t))
        )
        path = root / f"capture-{i}.csv"
        np.savetxt(path, np.column_stack([t, y]), delimiter=",", header="t,y", comments="")
        metadata = dict(
            time_column="t",
            time_unit="s",
            source_kind="synthetic",
            device="analytic-fixture",
            sample_interval_s=float(t[1] - t[0]),
            time_tolerance_s=1e-12,
            trigger_source="declared ideal step",
            trigger_threshold_v=0.5,
            time_origin="analytic exported time",
            session=f"acquisition-{i}",
            epoch=0,
            simultaneous=True,
            channels=[
                dict(
                    column="y",
                    unit="V",
                    measurement_point="synthetic observed combined output",
                    probe_ratio=1.0,
                    coupling="DC",
                    bandwidth_limit_hz=0.0,
                    probe_compensated=True,
                    acquisition_id=f"acquisition-{i}",
                )
            ],
        )
        meta = root / f"metadata-{i}.json"
        meta.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        experiments.append(
            dict(
                name=f"case-{i}",
                csv=path.name,
                metadata=meta.name,
                source_sha256=sha256_file(path),
                channel="y",
                partition=partition,
                direction=direction,
                step_time_s=step,
                window_s=[float(t[0]), float(t[-1])],
                standard_uncertainty_si=0.001,
                uncertainty_source="declared synthetic independent Gaussian sigma",
                baseline_si=baseline if partition == "validation" else None,
                amplitude_si=amplitude if partition == "validation" else None,
                levels_source="independent analytic input protocol; not fitted from held-out output"
                if partition == "validation"
                else None,
            )
        )
    return dict(
        format="zlscan.dynamic-fit-config/1",
        response_model="observed_single_exponential",
        error_model="independent_gaussian_known_standard_uncertainty",
        time_handling="conditional_on_declared_step_and_exported_times",
        assumption_source=(
            "synthetic analytic protocol, fixed seed 20261113; not real instrument transfer"
        ),
        tau_bounds_s=[0.0001, 0.05],
        confidence_level=0.95,
        rank_rtol=1e-10,
        validation_max_abs_residual_si=0.005,
        experiments=experiments,
    )


def verify(root):
    results = {}
    for name in ("nominal", "mismatch", "flat", "reused"):
        path = root / (name + ".json")
        saved = json.loads((root / name / "run.json").read_text(encoding="utf-8"))
        current = identify_dynamic_files(
            DynamicFitConfig.model_validate_json(path.read_bytes()), root
        )
        current["config_sha256"] = sha256_file(path)
        assert current == saved, name
        results[name] = current
        assert (
            current["origin"] == "MODEL_ESTIMATE"
            and not current["parameter_upgrades"]
            and not current["hard_acceptance_eligible"]
        )
    nominal = results["nominal"]
    assert nominal["status"] == "identified" and nominal["validation"]["status"] == "compatible"
    errors = {
        d: abs(nominal["directions"][d]["tau_s"] / expected - 1)
        for d, expected in [("rise", 0.002), ("fall", 0.005)]
    }
    assert max(errors.values()) < 0.01
    assert results["mismatch"]["directions"] == nominal["directions"]
    assert results["mismatch"]["validation"]["status"] == "mismatch"
    assert results["flat"]["directions"]["rise"]["tau_s"] is None
    assert results["reused"]["status"] == "rejected"
    manifest = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    for rel, digest in manifest.items():
        assert sha256_file(root / rel) == digest, rel
    return dict(
        origin="synthetic",
        seed=20261113,
        source_files_unchanged=len(manifest),
        cases=4,
        rise_tau_s=nominal["directions"]["rise"]["tau_s"],
        fall_tau_s=nominal["directions"]["fall"]["tau_s"],
        relative_errors=errors,
        criterion_relative_error=0.01,
        holdout_max_abs_residual_si=nominal["validation"]["max_abs_residual_si"],
        holdout_threshold_si=0.005,
        actual_measurements_obtained=False,
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
        print("all four CLI results independently recomputed; sources unchanged")
        return
    root.mkdir(parents=True, exist_ok=False)
    config = sources(root)
    configs = {"nominal": config}
    bad = json.loads(json.dumps(config))
    bad["experiments"][4]["baseline_si"] += 0.1
    configs["mismatch"] = bad
    flat = json.loads(json.dumps(config))
    case = flat["experiments"][0]
    path = root / "flat.csv"
    data = np.loadtxt(root / case["csv"], delimiter=",", skiprows=1)
    data[:, 1] = 0.3
    np.savetxt(path, data, delimiter=",", header="t,y", comments="")
    case["csv"] = path.name
    case["source_sha256"] = sha256_file(path)
    # Remove the other rising fit, so its actual information cannot rescue a flat experiment.
    flat["experiments"] = [e for e in flat["experiments"] if e["name"] != "case-2"]
    configs["flat"] = flat
    reused = json.loads(json.dumps(config))
    reused["experiments"][4].update(
        csv=config["experiments"][0]["csv"],
        metadata=config["experiments"][0]["metadata"],
        source_sha256=config["experiments"][0]["source_sha256"],
    )
    configs["reused"] = reused
    for name, value in configs.items():
        (root / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    manifest = {p.name: sha256_file(p) for p in sorted(root.iterdir()) if p.is_file()}
    (root / "sources.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    commands = []
    for name in configs:
        argv = [
            sys.executable,
            "-m",
            "zlscan",
            "ident",
            "--dynamic-fit",
            str(root / (name + ".json")),
            "--out",
            str(root / name),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        (root / (name + ".stdout")).write_text(result.stdout, encoding="utf-8")
        (root / (name + ".stderr")).write_text(result.stderr, encoding="utf-8")
        expected = 0 if name == "nominal" else 1
        commands.append(dict(argv=argv, returncode=result.returncode, expected_returncode=expected))
        (root / "commands.json").write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")
        assert result.returncode == expected, result.stdout + result.stderr
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n", encoding="utf-8")
    print("dynamic CLI, independent holdout, flat and reuse gates passed")


if __name__ == "__main__":
    main()
