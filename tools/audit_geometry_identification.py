"""Public geometric identification CLI with independent synthetic landmarks and held-out scans.

Annotations are explicit test inputs, not features detected from the constant-code raw rows.
Known generator coefficients are used only in this evaluator, never passed to identification.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np

from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.ident.geometry import GeometryFitConfig, identify_geometry_files
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import FLAG_LINE_END, FLAG_LINE_START, RawWriter, SessionHeader

TRUTH = [[0.012, 0.018, 0.0015, 0.00015], [0.011, 0.019, -0.0012, -0.00023]]


def generate(root):
    rng = np.random.default_rng(20261113)
    inputs = []
    for index, partition in enumerate(("fit", "fit", "validation")):
        raw = root / f"raw-{index}.zlraw"
        session = SessionHeader(
            schema_version="0.1",
            session_id=301 + index,
            created_at="2026-11-13T00:00:00Z",
            hardware_hash=None,
            firmware_hash=None,
            wiring_rev=None,
            register_dump_hash=None,
            calibration_id=None,
            channel_roles={"0": None},
            clock_domains=[dict(id=1, freq_num=1000000, freq_den=1, phase_ticks=0)],
            coding="offset_binary",
            bits_per_sample=16,
            bytes_per_sample=2,
            windows=dict(window_length=200, window_start=0, sync_latency_cycles=0),
            origin="SIMULATED",
        )
        timings, points = [], []
        with RawWriter(raw, session) as writer:
            for line in range(6):
                writer.write_block(
                    epoch=0,
                    channel_mask=1,
                    clock_domain=1,
                    first_sample_index=line * 400,
                    samples=np.full((200, 1), 30000, np.uint16),
                    flags=FLAG_LINE_START | FLAG_LINE_END,
                    line_number=line,
                )
                event = dict(
                    kind="line_sync",
                    origin="SIMULATED",
                    visibility="OBSERVABLE",
                    derivation="SENSOR",
                    clock_domain=1,
                    count=line,
                    tick=line * 400000000,
                    session_id=301 + index,
                    epoch=0,
                    capability_ref="synthetic/sync",
                )
                timings.append(
                    dict(
                        line=line,
                        sync=event,
                        anchor=dict(
                            event,
                            kind="facet_index",
                            capability_ref="synthetic/index",
                            payload={"phase": 0},
                        )
                        if line == 0
                        else None,
                    )
                )
                samples = [(u, 0.0) for u in np.linspace(-0.9, 0.9, 7)] + [
                    (u, 1.3) for u in [-0.75, -0.25, 0.25, 0.75]
                ]
                for j, (u, slope) in enumerate(samples):
                    a, b, c, dy = TRUTH[line % 2]
                    points.append(
                        dict(
                            name=f"line{line}-mark{j}",
                            line=line,
                            sample_column=float((u + 1) * 100 - 0.5),
                            target_x_m=float(
                                a + b * u + c * u**3 - slope * dy + rng.normal(0, 2e-7)
                            ),
                            target_dx_dy=slope,
                            standard_uncertainty_x_m=2e-7,
                            uncertainty_source="synthetic independent Gaussian target-x sigma",
                        )
                    )
        capture = CaptureDescription(
            raw_sha256=sha256_file(raw),
            lines=6,
            binding=Binding(
                simulation_hash="synthetic-geometry-instrument", channel_roles={"0": None}
            ),
        )
        cap = root / f"capture-{index}.json"
        cap.write_text(capture.model_dump_json(), encoding="utf-8")
        doc = dict(
            format="zlscan.geometry-observations/1",
            source_kind="synthetic",
            device="synthetic scanner",
            measurement_point="declared target markings",
            settings_source="explicit synthetic fixture",
            extraction_method="test annotations, not raw feature extraction",
            target_certificate_ref="synthetic dimensions, not a measured certificate",
            acquisition_id=f"scan-{index}",
            raw_path=raw.name,
            raw_sha256=sha256_file(raw),
            capture_path=cap.name,
            capture_sha256=sha256_file(cap),
            channel=0,
            timing=dict(
                raw_sha256=sha256_file(raw),
                policy=dict(
                    facets=2,
                    period_ticks=400000000,
                    tolerance_ticks=1,
                    phase_reference="synthetic/index",
                ),
                lines=timings,
            ),
            landmarks=points,
        )
        path = root / f"annotations-{index}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        inputs.append(
            dict(
                name=f"scan-{index}", path=path.name, sha256=sha256_file(path), partition=partition
            )
        )
    config = dict(
        format="zlscan.geometry-fit-config/1",
        confidence_level=0.95,
        rank_rtol=1e-10,
        assumption_source="independent synthetic target-x noise; fixed sample columns/slopes",
        input_handling="conditional_on_declared_landmarks_and_slopes",
        error_model="independent_gaussian_known_target_x_sigma",
        validation_max_abs_residual_m=3e-6,
        captures=inputs,
    )
    cases = {"identified": config}
    for name in ("grid_only", "mismatch", "no_anchor"):
        candidate = deepcopy(config)
        for row in candidate["captures"]:
            doc = json.loads((root / row["path"]).read_bytes())
            if name == "grid_only":
                doc["landmarks"] = [p for p in doc["landmarks"] if p["target_dx_dy"] == 0]
            if name == "mismatch" and row["partition"] == "validation":
                for p in doc["landmarks"]:
                    p["target_x_m"] += 0.002
            if name == "no_anchor":
                for entry in doc["timing"]["lines"]:
                    entry["anchor"] = None
            path = root / (name + "-" + row["path"])
            path.write_text(json.dumps(doc), encoding="utf-8")
            row.update(path=path.name, sha256=sha256_file(path))
        cases[name] = candidate
    for name, config in cases.items():
        (root / (name + ".json")).write_text(json.dumps(config), encoding="utf-8")
    (root / "assumptions.json").write_text(
        json.dumps(
            dict(
                source_kind="synthetic",
                coefficients_m=TRUTH,
                sigma_target_x_m=2e-7,
                evaluator_limit_standard_uncertainties=4,
                actual_measurements_obtained=False,
                annotations_detected_from_raw=False,
            )
        ),
        encoding="utf-8",
    )


def verify(root):
    errors = []
    for name, status, validation in [
        ("identified", "identified", "compatible"),
        ("grid_only", "partially_identifiable", "compatible"),
        ("mismatch", "identified", "mismatch"),
        ("no_anchor", "unidentifiable", "unidentifiable"),
    ]:
        path = root / (name + ".json")
        config = GeometryFitConfig.model_validate_json(path.read_bytes())
        recomputed = identify_geometry_files(config, root)
        recomputed["config_sha256"] = sha256_file(path)
        delivered = json.loads((root / name / "run.json").read_bytes())
        assert json.loads(json.dumps(recomputed)) == delivered
        assert delivered["status"] == status and delivered["validation"]["status"] == validation
        assert not delivered["parameter_upgrades"] and not delivered["hard_acceptance_eligible"]
        for ext in ("svg", "png"):
            assert (root / name / ("geometry_residuals." + ext)).stat().st_size > 1000
        if name == "identified":
            for phase, truth in enumerate(TRUTH):
                fit = delivered["facets"][str(phase)]
                for key, value in zip(fit["coefficient_order"], truth, strict=True):
                    p = fit["parameters"][key]
                    error = abs(p["value"] - value) / p["standard_uncertainty"]
                    assert error < 4
                    errors.append(error)
    return dict(
        format="zlscan.geometry-identification-audit/1",
        cases=4,
        maximum_error_standard_uncertainties=max(errors),
        actual_measurements_obtained=False,
        parameter_upgrades=[],
        scope="synthetic conditional geometry; not measured target extraction or hardware",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.out.resolve()
    if args.verify:
        assert verify(root) == json.loads((root / "audit.json").read_bytes())
        print("Fresh-process geometric identification readback passed")
        return
    root.mkdir(parents=True, exist_ok=False)
    generate(root)
    for name, expected in [("identified", 0), ("grid_only", 1), ("mismatch", 1), ("no_anchor", 1)]:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "zlscan",
                "ident",
                "--geometry-fit",
                str(root / (name + ".json")),
                "--out",
                str(root / name),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        (root / (name + ".stdout")).write_text(proc.stdout, encoding="utf-8")
        (root / (name + ".stderr")).write_text(proc.stderr, encoding="utf-8")
        assert proc.returncode == expected, (name, proc.stderr)
    (root / "audit.json").write_text(json.dumps(verify(root), indent=2) + "\n", encoding="utf-8")
    print("Public geometric identification audit passed")


if __name__ == "__main__":
    main()
