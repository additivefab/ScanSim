"""Importer safeguards use synthetic fixtures, never hardware evidence."""

import hashlib
import json

import pytest

from zlscan.cli import main
from zlscan.ident.scope import ScopeMetadata, import_scope_csv


def metadata():
    return {
        "time_column": "t",
        "time_unit": "us",
        "device": "synthetic scope",
        "sample_interval_s": 1e-6,
        "time_tolerance_s": 1e-12,
        "trigger_source": "CH1",
        "trigger_threshold_v": 0.0,
        "time_origin": "trigger",
        "session": "fixture",
        "epoch": 0,
        "simultaneous": True,
        "source_kind": "synthetic",
        "channels": [
            dict(
                column=c,
                unit="mV",
                measurement_point=c,
                probe_ratio=10.0,
                coupling="DC",
                bandwidth_limit_hz=0.0,
                probe_compensated=True,
                acquisition_id="one",
            )
            for c in ["CH1", "CH2"]
        ],
    }


def capture(tmp_path, content="t,CH1,CH2\n-1,10,20\n0,30,40\n1,50,60\n"):
    p = tmp_path / "scope.csv"
    p.write_text(content)
    return p


def test_units_identity_and_no_automatic_evidence_upgrade(tmp_path):
    p = capture(tmp_path)
    result = import_scope_csv(p, ScopeMetadata.model_validate(metadata()))
    assert result["time_s"] == [-1e-6, 0.0, 1e-6]
    assert result["channels"][0]["values_si"] == [0.01, 0.03, 0.05]
    assert result["source_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert result["synchronization"] == "declared_same_acquisition"
    assert not result["hard_acceptance_eligible"]
    assert result["parameter_upgrades"] == []


def test_missing_metadata_is_view_only_and_unknowns_remain_null(tmp_path):
    m = {"time_column": "t", "time_unit": "us", "channels": [{"column": "CH1", "unit": "mV"}]}
    r = import_scope_csv(capture(tmp_path), ScopeMetadata.model_validate(m))
    assert "device" in r["missing_metadata"]
    assert "channels.0.probe_ratio" in r["missing_metadata"]
    assert r["metadata"]["epoch"] is None
    assert r["synchronization"] == "unestablished"


@pytest.mark.parametrize("change", ["different_id", "not_simultaneous", "interval"])
def test_multichannel_sync_cannot_be_inferred_from_shared_csv(tmp_path, change):
    m = metadata()
    if change == "different_id":
        m["channels"][1]["acquisition_id"] = "two"
    elif change == "not_simultaneous":
        m["simultaneous"] = False
    else:
        m["sample_interval_s"] = 2e-6
    r = import_scope_csv(capture(tmp_path), ScopeMetadata.model_validate(m))
    assert r["synchronization"] != "declared_same_acquisition"
    if change == "interval":
        assert r["issues"]


@pytest.mark.parametrize(
    "body",
    [
        "t,CH1,CH2\n0,1,2\n0,3,4\n",
        "t,CH1,CH2\n0,1,2\n1,nan,3\n",
        "t,CH1,CH1\n0,1,2\n1,3,4\n",
        "t,CH1,CH2\n0,1,2,3\n1,3,4\n",
    ],
)
def test_corrupt_csv_rejected(tmp_path, body):
    with pytest.raises(ValueError):
        import_scope_csv(capture(tmp_path, body), ScopeMetadata.model_validate(metadata()))


def test_cli_preserves_inputs_and_refuses_overwrite(tmp_path):
    p = capture(tmp_path)
    m = tmp_path / "metadata.json"
    m.write_text(json.dumps(metadata()))
    before = (p.read_bytes(), m.read_bytes())
    out = tmp_path / "import"
    args = ["ident", "--csv", str(p), "--metadata", str(m), "--out", str(out)]
    assert main(args) == 0
    r = json.loads((out / "run.json").read_text())
    assert r["source_kind"] == "synthetic"
    assert main(args) != 0
    assert before == (p.read_bytes(), m.read_bytes())


def test_metadata_rejects_units_typos_and_unknown_fields():
    m = metadata()
    m["time_unit"] = "ticks"
    with pytest.raises(ValueError):
        ScopeMetadata.model_validate(m)
    m = metadata()
    m["channels"][0]["probe_ratio"] = 0
    with pytest.raises(ValueError):
        ScopeMetadata.model_validate(m)
