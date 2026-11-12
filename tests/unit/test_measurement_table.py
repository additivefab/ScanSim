"""Scalar tables preserve missing data, uncertainty semantics and held-out lineage."""

import csv
import hashlib
import json

import pytest

from zlscan.cli import main
from zlscan.ident.table import COLUMNS, TableMetadata, import_measurement_table


def row(**changes):
    r = dict.fromkeys(COLUMNS, "")
    r.update(
        measurement_id="m1",
        reference_id="white",
        partition="fit",
        quantity="voltage",
        value="2500",
        unit="mV",
        uncertainty="10",
        uncertainty_kind="standard",
        uncertainty_source="synthetic declaration",
        session="fixture",
        epoch="0",
        acquisition_id="one",
    )
    r.update(changes)
    return r


def table(tmp_path, rows):
    p = tmp_path / "table.csv"
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return p


def test_si_units_uncertainty_and_missing_source_are_not_certification(tmp_path):
    p = table(tmp_path, [row()])
    r = import_measurement_table(p, TableMetadata(), tmp_path)
    assert r["rows"][0]["value_si"] == 2.5
    assert r["rows"][0]["uncertainty_si"] == 0.01
    assert r["rows"][0]["declared"]["uncertainty_kind"] == "standard"
    assert r["rows"][0]["declared"]["coverage_factor"] is None
    assert not r["rows"][0]["source_verified"]
    assert r["partition_status"] == "not_assessed"
    assert not r["hard_acceptance_eligible"]
    assert r["parameter_upgrades"] == []
    assert r["source_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_blank_value_and_uncertainty_remain_null(tmp_path):
    r = import_measurement_table(
        table(tmp_path, [row(value="", uncertainty="")]), TableMetadata(), tmp_path
    )
    assert r["rows"][0]["value_si"] is None
    assert r["rows"][0]["uncertainty_si"] is None
    assert "value" in r["rows"][0]["missing_metadata"]
    assert r["metadata"]["sample_interval_s"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"unit": "A"},
        {"value": "nan"},
        {"uncertainty": "-1"},
        {"epoch": "-1"},
        {"partition": "test"},
        {"coverage_factor": "inf"},
        {"quantity": "unknown"},
    ],
)
def test_invalid_table_values_are_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        import_measurement_table(table(tmp_path, [row(**changes)]), TableMetadata(), tmp_path)


def test_duplicate_measurement_ids_are_rejected(tmp_path):
    with pytest.raises(ValueError):
        import_measurement_table(table(tmp_path, [row(), row()]), TableMetadata(), tmp_path)


def test_same_source_cannot_be_declared_independent_fit_and_validation(tmp_path):
    raw = tmp_path / "source.dat"
    raw.write_bytes(b"synthetic observation")
    sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    m = TableMetadata(source_files=[{"path": "source.dat", "sha256": sha}])
    p = table(
        tmp_path,
        [
            row(source_sha256=sha),
            row(
                measurement_id="m2", partition="validation", source_sha256=sha, acquisition_id="two"
            ),
        ],
    )
    r = import_measurement_table(p, m, tmp_path)
    assert all(x["source_verified"] for x in r["rows"])
    assert r["partition_status"] == "leakage_detected"
    assert r["leakage"][0]["kind"] == "source_sha256"


def test_same_capture_relabelled_as_different_files_is_still_detected(tmp_path):
    r = import_measurement_table(
        table(tmp_path, [row(), row(measurement_id="m2", partition="validation")]),
        TableMetadata(),
        tmp_path,
    )
    assert r["partition_status"] == "leakage_detected"
    assert any(x["kind"] == "capture_identity" for x in r["leakage"])


def test_expanded_uncertainty_needs_explicit_factor_and_never_becomes_bound(tmp_path):
    r = import_measurement_table(
        table(tmp_path, [row(uncertainty_kind="expanded")]), TableMetadata(), tmp_path
    )
    assert "coverage_factor" in r["rows"][0]["missing_metadata"]
    assert r["rows"][0]["uncertainty_si"] == 0.01


def test_declared_source_must_match_actual_bytes(tmp_path):
    (tmp_path / "source.dat").write_bytes(b"a")
    m = TableMetadata(source_files=[{"path": "source.dat", "sha256": "0" * 64}])
    with pytest.raises(ValueError, match="SHA"):
        import_measurement_table(table(tmp_path, [row()]), m, tmp_path)


def test_cli_table_import_is_read_only_and_refuses_overwrite(tmp_path):
    p = table(tmp_path, [row()])
    meta = tmp_path / "meta.json"
    meta.write_text("{}")
    before = (p.read_bytes(), meta.read_bytes())
    out = tmp_path / "out"
    args = ["ident", "--table", str(p), "--metadata", str(meta), "--out", str(out)]
    assert main(args) == 0
    r = json.loads((out / "run.json").read_text())
    assert r["format"] == "zlscan.measurement-table/1"
    assert r["synchronization"] == "not_applicable_scalar_table"
    assert main(args) != 0
    assert before == (p.read_bytes(), meta.read_bytes())


def test_disjoint_declared_sources_do_not_prove_statistical_independence(tmp_path):
    sources = []
    rows = []
    for i, role in enumerate(["fit", "validation"]):
        p = tmp_path / f"{role}.dat"
        p.write_bytes(f"synthetic-{role}".encode())
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        sources.append({"path": p.name, "sha256": digest})
        rows.append(
            row(
                measurement_id=f"m{i}",
                partition=role,
                source_sha256=digest,
                acquisition_id=role,
                uncertainty_kind="expanded",
                coverage_factor="2",
            )
        )
    r = import_measurement_table(
        table(tmp_path, rows), TableMetadata(source_files=sources), tmp_path
    )
    assert r["partition_status"] == "no_declared_reuse_detected"
    assert not r["independence_established"]
    assert r["rows"][0]["uncertainty_si"] == 0.01
    assert r["rows"][0]["declared"]["coverage_factor"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        "measurement_id,measurement_id\nm1,m2\n",
        ",".join(COLUMNS) + "\nm1,white,fit\n",
    ],
)
def test_bad_table_headers_and_ragged_rows_rejected(tmp_path, payload):
    p = tmp_path / "table.csv"
    p.write_text(payload)
    with pytest.raises(ValueError):
        import_measurement_table(p, TableMetadata(), tmp_path)


def test_out_of_range_observation_is_preserved_and_flagged(tmp_path):
    r = import_measurement_table(
        table(
            tmp_path,
            [
                row(
                    quantity="transmittance",
                    value="1.1",
                    unit="1",
                    uncertainty_kind="absolute_bound",
                )
            ],
        ),
        TableMetadata(),
        tmp_path,
    )
    assert r["rows"][0]["value_si"] == 1.1
    assert r["rows"][0]["issues"]
    assert r["rows"][0]["declared"]["uncertainty_kind"] == "absolute_bound"
