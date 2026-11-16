"""Observed feed-encoder counts decide the row positions of a reconstruction (N3)."""

import json

import numpy as np
import pytest
from tests.unit.test_density_reconstruction import calibration

from zlscan.contracts.calibration import Binding, CaptureDescription
from zlscan.io.manifest import sha256_file
from zlscan.io.rawstream import (
    FLAG_EPOCH_START,
    FLAG_LINE_END,
    FLAG_LINE_START,
    RawWriter,
    SessionHeader,
)
from zlscan.recon.capture import reconstruct_capture
from zlscan.recon.density import Grid
from zlscan.recon.encoder import FeedEncoder

LEVELS = (10, 14, 20)  # one flat raw code per line


def fixture(tmp_path):
    session = SessionHeader(
        schema_version="0.1", session_id=21, created_at="2026-11-16T00:00:00Z",
        hardware_hash=None, firmware_hash=None, wiring_rev=None, register_dump_hash=None,
        calibration_id=None, channel_roles={"0": "signal"},
        clock_domains=[dict(id=1, freq_num=1_000_000, freq_den=1, phase_ticks=0)],
        coding="offset_binary", bits_per_sample=16, bytes_per_sample=2,
        windows=dict(window_length=6), origin="SIMULATED",
    )  # fmt: skip
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session) as writer:
        for k, level in enumerate(LEVELS):
            writer.write_block(
                epoch=0, channel_mask=1, clock_domain=1, first_sample_index=6 * k, line_number=k,
                samples=np.full((6, 1), level, np.uint16),
                flags=FLAG_LINE_START | FLAG_LINE_END | (FLAG_EPOCH_START if k == 0 else 0),
            )  # fmt: skip
    binding = Binding(
        simulation_hash="bench", channel_roles={"0": "signal"},
        gain_settings={"0": "fixed_assumed"}, epoch_range=(0, 0),
    )  # fmt: skip
    cal = calibration()
    cal.binds_to = binding
    cal.items["dark_raw_code"].value["0"]["median"] = [0] * 6
    cal.items["white_raw_code"].value["0"]["median"] = [20] * 6
    capture = CaptureDescription(raw_sha256=sha256_file(path), binding=binding, lines=3)
    return path, capture, cal


def encoder_file(tmp_path, raw, counts=(0, 1, 2), **over):
    doc = dict(format="zlscan-feed-encoder/1", raw_sha256=sha256_file(raw), pitch_m=1.0,
               counts=list(counts), origin="SIMULATED") | over  # fmt: skip
    path = tmp_path / "encoder.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def rows(output):
    with np.load(output / "density.npz") as data:
        return data["transmittance"], data["flags"]


def test_uniform_encoder_counts_reproduce_the_nominal_placement(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    nominal = tmp_path / "nominal"
    reconstruct_capture(raw, capture, cal, Grid(6, 3, 1, 1), nominal)
    observed = tmp_path / "observed"
    reconstruct_capture(raw, capture, cal, Grid(6, 3, 1, 1), observed,
                        encoder_path=encoder_file(tmp_path, raw, [0, 1, 2]))  # fmt: skip
    np.testing.assert_allclose(rows(observed)[0], rows(nominal)[0])
    plan = json.loads((observed / "lineage.json").read_text())
    assert {line["y_source"] for line in plan["lines"]} == {"encoder"}
    assert [line["y_m"] for line in plan["lines"]] == [0.5, 1.5, 2.5]
    assert "y_source" not in json.loads((nominal / "lineage.json").read_text())["lines"][0]


def test_stalled_feed_puts_two_lines_on_the_same_row(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    output = tmp_path / "stalled"
    reconstruct_capture(raw, capture, cal, Grid(6, 3, 1, 1), output,
                        encoder_path=encoder_file(tmp_path, raw, [0, 1, 1]))  # fmt: skip
    plan = json.loads((output / "lineage.json").read_text())
    assert [line["y_m"] for line in plan["lines"]] == [0.5, 1.5, 1.5]
    t, flags = rows(output)
    nominal = tmp_path / "nominal"
    reconstruct_capture(raw, capture, cal, Grid(6, 3, 1, 1), nominal)
    n, _ = rows(nominal)
    np.testing.assert_allclose(t[0], n[0])
    np.testing.assert_allclose(t[1], (n[1] + n[2]) / 2)  # two lines landed on one row
    assert flags[2].all() and not flags[0].any()  # the row the feed never reached is invalid


@pytest.mark.parametrize(
    "bad",
    [
        dict(counts=[0, 2, 1]),
        dict(counts=[0, 1]),
        dict(pitch_m=0.0),
        dict(raw_sha256="0" * 64),
    ],
)
def test_bad_encoder_inputs_are_rejected(tmp_path, bad):
    raw, capture, cal = fixture(tmp_path)
    path = encoder_file(tmp_path, raw, **bad)
    with pytest.raises(ValueError):
        reconstruct_capture(raw, capture, cal, Grid(6, 3, 1, 1), tmp_path / "x", encoder_path=path)


def test_encoder_is_refused_with_epoch_calibration(tmp_path):
    raw, capture, cal = fixture(tmp_path)
    capture = capture.model_copy(update={"version": 2})
    with pytest.raises(ValueError):
        reconstruct_capture(raw, capture, cal, Grid(6, 3, 1, 1), tmp_path / "e",
                            encoder_path=encoder_file(tmp_path, raw, [0, 1, 2]))  # fmt: skip


def test_encoder_model_validation():
    FeedEncoder(format="zlscan-feed-encoder/1", raw_sha256="a", pitch_m=2e-5, counts=[5, 5, 9],
                origin="OBSERVED")  # fmt: skip
    with pytest.raises(ValueError):
        FeedEncoder(format="zlscan-feed-encoder/1", raw_sha256="a", pitch_m=1.0, counts=[],
                    origin="OBSERVED")  # fmt: skip
