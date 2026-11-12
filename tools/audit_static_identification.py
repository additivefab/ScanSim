"""Synthetic file-based static identification audit; no hardware measurements."""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from pathlib import Path

from zlscan.ident.static import StaticFitConfig, identify_static
from zlscan.ident.table import COLUMNS, TableMetadata, import_measurement_table
from zlscan.io.manifest import sha256_file


def build_case(directory: Path, name: str) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=False)
    feature_count = 2 if name == "collinear" else 1
    rows, sources, observations = [], [], []
    ys = [1.04, 2.97, 5.08, 6.95]
    val_y = 4.5 if name == "mismatch" else 4.01
    for role, points in [
        ("fit", [(float(i), y) for i, y in enumerate(ys)]),
        ("validation", [(1.5, val_y)]),
    ]:
        for i, (x, y) in enumerate(points):
            key = f"{role}{i}"
            inputs = [x] * feature_count
            if name == "collinear" and role == "validation":
                inputs = [1.0, 2.0]
            raw = directory / f"{key}.json"
            raw.write_text(json.dumps(dict(origin="SYNTHETIC", input_v=inputs, output_v=y)))
            sha = sha256_file(raw)
            sources.append(dict(path=raw.name, sha256=sha))
            ids = {}
            for j, value in enumerate([*inputs, y]):
                mid = f"{key}_{j}"
                row = dict.fromkeys(COLUMNS, "")
                row.update(
                    measurement_id=mid,
                    reference_id=key,
                    partition=role,
                    quantity="voltage",
                    unit="V",
                    value=str(value),
                    uncertainty="0.0" if j < feature_count else "0.1",
                    uncertainty_kind="standard",
                    uncertainty_source="synthetic known sigma assumption",
                    source_sha256=sha,
                    session=f"synthetic:{name}",
                    epoch="0",
                    acquisition_id=key,
                )
                rows.append(row)
                if j < feature_count:
                    ids[f"x{j}"] = mid
            observations.append(dict(inputs=ids, output=mid))
    table = directory / "measurements.csv"
    with table.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    metadata = directory / "metadata.json"
    metadata.write_text(
        TableMetadata(
            source_kind="synthetic", device="synthetic generator", source_files=sources
        ).model_dump_json()
    )
    config = directory / "static.json"
    config.write_text(
        StaticFitConfig(
            features=[dict(name=f"x{j}", scale_si=1.0) for j in range(feature_count)],
            intercept=True,
            confidence_level=0.95,
            rank_rtol=1e-10,
            input_handling="conditional_on_declared_values",
            output_error_model="independent_gaussian_known_standard_uncertainty",
            assumption_source="synthetic conditional regression formula audit",
            observations=observations,
            validation_max_abs_residual_si=0.1,
        ).model_dump_json()
    )
    return table, metadata, config


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    summary = {}
    for name in ["compatible", "mismatch", "collinear"]:
        directory = output / name
        table, metadata, config = build_case(directory / "inputs", name)
        inputs = {
            str(p.relative_to(directory)): sha256_file(p) for p in (directory / "inputs").iterdir()
        }
        argv = [
            sys.executable,
            "-m",
            "zlscan",
            "ident",
            "--table",
            str(table),
            "--metadata",
            str(metadata),
            "--static-fit",
            str(config),
            "--out",
            str(directory / "result"),
        ]
        p = subprocess.run(argv, capture_output=True, text=True)
        (directory / "cli.log").write_text(p.stdout + p.stderr)
        assert p.returncode == (0 if name == "compatible" else 1), p.stderr
        assert inputs == {k: sha256_file(directory / k) for k in inputs}
        report = json.loads((directory / "result/run.json").read_text())["static_identification"]
        assert report["status"] == (
            "partially_identifiable" if name == "collinear" else "identified"
        )
        assert report["validation"]["status"] == (
            "unidentifiable"
            if name == "collinear"
            else "mismatch"
            if name == "mismatch"
            else "compatible"
        )
        if name != "collinear":
            assert math.isclose(report["parameters"]["intercept"]["value"], 1.034, abs_tol=1e-12)
            assert math.isclose(report["parameters"]["x0"]["value"], 1.984, abs_tol=1e-12)
        else:
            assert report["parameters"]["x0"]["value"] is None
            assert report["parameters"]["x1"]["value"] is None
        assert not report["hard_acceptance_eligible"] and report["parameter_upgrades"] == []
        summary[name] = dict(
            returncode=p.returncode,
            status=report["status"],
            validation=report["validation"],
            parameters=report["parameters"],
            input_sha256=inputs,
        )
    assert summary["compatible"]["parameters"] == summary["mismatch"]["parameters"]
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def verify(output: Path) -> None:
    summary = json.loads((output / "summary.json").read_text())
    for name, case in summary.items():
        directory = output / name
        assert case["input_sha256"] == {k: sha256_file(directory / k) for k in case["input_sha256"]}
        base = directory / "inputs"
        meta = TableMetadata.model_validate_json((base / "metadata.json").read_bytes())
        table = import_measurement_table(base / "measurements.csv", meta, base)
        cfg = StaticFitConfig.model_validate_json((base / "static.json").read_bytes())
        actual = identify_static(table, cfg)
        saved = json.loads((directory / "result/run.json").read_text())["static_identification"]
        assert actual == saved, f"{name}: fresh process analysis differs"
    print("Three synthetic cases re-read exactly; original input hashes unchanged")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--out", type=Path)
    group.add_argument("--verify", type=Path)
    args = p.parse_args()
    if args.verify is not None:
        verify(args.verify)
    else:
        run(args.out)
        print("Saved compatible, mismatching and unidentifiable synthetic cases")


if __name__ == "__main__":
    main()
