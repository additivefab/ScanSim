"""ZLRAW import fixtures are synthetic and preserve observable identities."""

import hashlib
import json

import numpy as np
import pytest

from zlscan.cli import main
from zlscan.ident.raw import RawMetadata, import_raw_capture
from zlscan.io.rawstream import FLAG_EPOCH_START, RawWriter, SessionHeader


def capture(tmp_path, *, gap=False, epochs=False, origin="SIMULATED", count=3, domains=False):
    p = tmp_path / "capture.zlraw"
    session = SessionHeader(
        schema_version="0.1",
        session_id=7,
        created_at="synthetic fixture",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": None, "1": None},
        clock_domains=[],
        coding="twos_complement",
        bits_per_sample=16,
        bytes_per_sample=2,
        origin=origin,
        windows={},
    )
    with RawWriter(p, session) as w:
        for i in range(2):
            w.write_block(
                epoch=i if epochs else 0,
                channel_mask=3,
                clock_domain=i if domains else 0,
                first_sample_index=2**53 + i * (count + (2 if gap else 0)),
                flags=FLAG_EPOCH_START if epochs else 0,
                samples=np.full((count, 2), 32768 + i, dtype="<u2"),
            )
    return p


def declared():
    return dict(
        device="fixture",
        source_kind="synthetic",
        session_id=7,
        epochs=[0],
        sample_interval_s=1e-6,
        trigger_source="external",
        trigger_threshold_v=0.0,
        time_origin="session start",
        simultaneous=True,
        channels=[
            dict(
                channel=i,
                measurement_point=f"ADC{i}",
                probe_ratio=1.0,
                coupling="DC",
                bandwidth_limit_hz=0.0,
                acquisition_id="one",
            )
            for i in [0, 1]
        ],
    )


def test_raw_snapshot_preserves_words_large_identities_and_unknowns(tmp_path):
    source = capture(tmp_path, epochs=True)
    before = source.read_bytes()
    out = tmp_path / "import"
    report = import_raw_capture(source, RawMetadata(), out)
    assert (out / "raw.zlraw").read_bytes() == before == source.read_bytes()
    assert report["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert report["session"]["coding"] == "twos_complement"
    assert report["session"]["hardware_hash"] is None
    assert report["blocks"][0]["first_sample_index"] == 2**53
    assert [b["epoch"] for b in report["blocks"]] == [0, 1]
    assert report["preview"][0]["codes"][0] == [32768, 32768]
    assert "device" in report["missing_metadata"]
    assert report["synchronization"] == "unestablished"
    assert report["parameter_upgrades"] == []
    assert not report["hard_acceptance_eligible"]
    assert report["integrity"] == "seen_range_checked"


def test_declared_synchronization_does_not_certify_hardware(tmp_path):
    report = import_raw_capture(capture(tmp_path), RawMetadata(**declared()), tmp_path / "out")
    assert report["synchronization"] == "declared_same_acquisition"
    assert not report["hard_acceptance_eligible"]


@pytest.mark.parametrize("issue", ["source", "session", "epoch", "channel", "acquisition", "gap"])
def test_conflicting_metadata_and_loss_prevent_synchronized_use(tmp_path, issue):
    m = declared()
    if issue == "source":
        m["source_kind"] = "declared_measurement"
    elif issue == "session":
        m["session_id"] = 8
    elif issue == "epoch":
        m["epochs"] = [1]
    elif issue == "channel":
        m["channels"][1]["channel"] = 2
    elif issue == "acquisition":
        m["channels"][1]["acquisition_id"] = "two"
    r = import_raw_capture(
        capture(tmp_path, gap=issue == "gap"), RawMetadata(**m), tmp_path / "out"
    )
    assert r["synchronization"] != "declared_same_acquisition"
    assert r["session"]["origin"] == "SIMULATED"
    if issue == "gap":
        assert r["losses"][0]["first_sample_index"] == 2**53 + 3
        assert r["integrity"] == "incomplete"
    else:
        assert r["issues"]


def test_crc_bad_data_not_previewed_and_source_preserved(tmp_path):
    p = capture(tmp_path)
    data = bytearray(p.read_bytes())
    data[-1] ^= 1
    p.write_bytes(data)
    r = import_raw_capture(p, RawMetadata(), tmp_path / "out")
    assert r["integrity"] == "incomplete"
    assert r["errors"][0]["code"] == "payload_crc"
    assert len(r["preview"]) == 1
    assert (tmp_path / "out/raw.zlraw").read_bytes() == data


def test_preview_bounded_independently_of_sample_count(tmp_path):
    r = import_raw_capture(capture(tmp_path, count=300), RawMetadata(), tmp_path / "out")
    assert sum(len(p["codes"]) for p in r["preview"]) == 256
    assert r["sample_rows"] == 600


def test_unknown_protocol_cli_fails_with_preserved_diagnostic_and_no_overwrite(tmp_path):
    source = tmp_path / "unknown.bin"
    source.write_bytes(b"unknown FPGA protocol")
    meta = tmp_path / "meta.json"
    meta.write_text("{}")
    out = tmp_path / "out"
    args = ["ident", "--raw", str(source), "--metadata", str(meta), "--out", str(out)]
    assert main(args) != 0
    r = json.loads((out / "run.json").read_text())
    assert r["integrity"] == "unreadable"
    assert source.read_bytes() == (out / "raw.zlraw").read_bytes()
    assert main(args) != 0


def test_metadata_refuses_duplicate_channels_and_invalid_epoch():
    with pytest.raises(ValueError):
        RawMetadata(channels=[{"channel": 0}, {"channel": 0}])
    with pytest.raises(ValueError):
        RawMetadata(epochs=[-1])


def test_multiple_domains_and_explicit_non_simultaneous_are_not_aligned(tmp_path):
    m = declared()
    r = import_raw_capture(capture(tmp_path, domains=True), RawMetadata(**m), tmp_path / "domains")
    assert r["synchronization"] == "unestablished"
    assert "multiple_clock_domains; alignment_not_established" in r["issues"]
    m["simultaneous"] = False
    r = import_raw_capture(tmp_path / "capture.zlraw", RawMetadata(**m), tmp_path / "separate")
    assert r["synchronization"] == "non_simultaneous"


def test_cli_raw_success_retains_declared_provenance_without_upgrading(tmp_path):
    source = capture(tmp_path, origin="MEASURED")
    m = declared()
    m["source_kind"] = "declared_measurement"
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps(m))
    out = tmp_path / "import"
    assert main(["ident", "--raw", str(source), "--metadata", str(meta), "--out", str(out)]) == 0
    r = json.loads((out / "run.json").read_text())
    assert r["session"]["origin"] == "MEASURED"
    assert r["metadata_sha256"] == hashlib.sha256(meta.read_bytes()).hexdigest()
    assert not r["hard_acceptance_eligible"]
    assert r["parameter_upgrades"] == []
    # MEASURED above is merely a synthetic test header, never actual device evidence.
