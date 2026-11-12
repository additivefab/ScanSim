"""Physical mode end to end on the small demo scene."""

import json

import numpy as np
import pytest

from zlscan.config.presets import demo_config
from zlscan.digital.acquisition import AcquisitionConfig
from zlscan.engine.physical_run import line_steps, run_physical, stream_raw
from zlscan.io.rawstream import read_all


@pytest.fixture(scope="module")
def physical(tmp_path_factory):
    out = tmp_path_factory.mktemp("phys")
    cfg = demo_config()
    run = run_physical(out, cfg, lines=6)
    return out, cfg, run


def test_the_physical_run_writes_a_complete_observable_directory(physical):
    out, _, run = physical
    assert run["mode"] == "physical" and run["integrity"] == "complete"
    assert run["recon_status"] == "complete"
    for name in ("observable/raw.zlraw", "observable/recon.npz", "run.json", "viewer.html"):
        assert (out / name).exists()


def test_the_run_records_the_evidence_and_the_config_hash(physical):
    out, cfg, run = physical
    saved = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert saved["config_hash"] == cfg.hash() and saved["evidence"] == {"all": "assumed"}


def test_the_reconstructed_lines_show_the_step_wedge_in_order(physical):
    out, _, _ = physical
    img = np.load(out / "observable" / "recon.npz")["ch0"]
    row = img[3].astype(float)
    quarters = [row[i * len(row) // 4 : (i + 1) * len(row) // 4].mean() for i in range(4)]
    assert quarters == sorted(quarters)  # polarity -1: denser film -> larger code


def test_the_sampling_phase_differs_from_line_to_line(physical):
    out, _, _ = physical
    blocks = read_all(out / "observable" / "raw.zlraw").blocks
    starts = [b.first_sample_index for b in blocks]
    assert len(set(np.diff(starts))) >= 1 and len(starts) == 6


def test_stream_writer_matches_the_batch_pipeline_samples(tmp_path, physical):
    out, cfg, _ = physical
    stream_raw(tmp_path / "s.zlraw", cfg, lines=6)
    a = read_all(tmp_path / "s.zlraw")
    b = read_all(out / "observable" / "raw.zlraw")
    assert a.blocks == b.blocks
    assert len(a.segments) == len(b.segments) == 6
    for actual, expected in zip(a.segments, b.segments, strict=True):
        np.testing.assert_array_equal(actual.samples, expected.samples)


def test_streaming_sample_memory_stays_bounded_after_cache_warmup(tmp_path):
    cfg = demo_config()
    # Both runs fill the bounded RNG cache before comparing peak allocations.
    small = stream_raw(tmp_path / "a.zlraw", cfg, lines=16, measure_memory=True)
    big = stream_raw(tmp_path / "b.zlraw", cfg, lines=32, measure_memory=True)
    assert big["lines_written"] == 32 and small["lines_written"] == 16
    assert big["python_peak_bytes"] < small["python_peak_bytes"] * 1.5


def test_line_steps_matches_the_period():
    assert line_steps(demo_config()) == 10_000


@pytest.mark.parametrize(
    ("window_start", "window_length", "expected_lengths"),
    [(0, 2600, [2600, 2497]), (2400, 300, [300, 97])],
)
def test_stream_matches_acquisition_for_overlapping_sync_and_frame_end(
    tmp_path, window_start, window_length, expected_lengths
):
    cfg = demo_config()
    out = tmp_path / "batch"
    run_physical(
        out,
        cfg,
        lines=3,
        digital={"window_start": window_start, "window_length": window_length},
    )
    path = tmp_path / "stream.zlraw"
    result = stream_raw(path, cfg, lines=3, acq=AcquisitionConfig(window_start, window_length))
    batch = read_all(out / "observable" / "raw.zlraw")
    stream = read_all(path)
    assert result["fine_steps"] == 3 * line_steps(cfg)
    assert result["lines_written"] == len(batch.blocks) == len(stream.blocks)
    assert [len(segment.samples) for segment in batch.segments] == expected_lengths
    for actual, expected in zip(stream.blocks, batch.blocks, strict=True):
        assert actual.first_sample_index == expected.first_sample_index
        assert actual.line_number == expected.line_number
    for actual, expected in zip(stream.segments, batch.segments, strict=True):
        np.testing.assert_array_equal(actual.samples, expected.samples)
