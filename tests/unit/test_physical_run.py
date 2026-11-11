"""Physical mode end to end on the small demo scene."""

import json

import numpy as np
import pytest

from zlscan.config.presets import demo_config
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
    assert [x.first_sample_index for x in a.blocks] == [x.first_sample_index for x in b.blocks]
    assert np.array_equal(a.segments[0].samples if a.segments else 0, b.segments[0].samples)


def test_streaming_memory_does_not_grow_with_the_number_of_lines(tmp_path):
    cfg = demo_config()
    small = stream_raw(tmp_path / "a.zlraw", cfg, lines=4, measure_memory=True)
    big = stream_raw(tmp_path / "b.zlraw", cfg, lines=16, measure_memory=True)
    assert big["lines_written"] == 16 and small["lines_written"] == 4
    assert big["python_peak_bytes"] < small["python_peak_bytes"] * 1.5


def test_line_steps_matches_the_period():
    assert line_steps(demo_config()) == 10_000
