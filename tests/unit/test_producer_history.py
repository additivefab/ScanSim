"""Original accepted inputs, code fingerprints and pipeline completion hashes are committed."""

import numpy as np
import pytest

from zlscan.adc.patterns import ConvertedSamples
from zlscan.contracts.time import ClockDomain


def header():
    return dict(
        format="zlscan-producer-history/1",
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        os=4,
        channels=[0],
        bits=16,
        identity=dict(run_id="r", branch_id="b", session_id=19),
        fine_clock=dict(id=1, freq_num=4_000_000, freq_den=1, phase_ticks=0),
        digital_config_hash="a" * 64,
        initial_pipeline_hash="b" * 64,
    )


def test_original_syncs_batches_finish_and_prefix_branch(tmp_path):
    from zlscan.io.producer_history import HistoryReader, HistoryWriter, describe_source

    path = tmp_path / "history.jsonl"
    source = ConvertedSamples(
        np.arange(3, dtype=np.uint16)[:, None],
        (0,),
        0,
        2,
        ClockDomain(1, 1_000_000),
        16,
        "r",
        "b",
        19,
    )
    with HistoryWriter(path, header()) as writer:
        writer.sync([0, 2000000], "c" * 64)
        writer.advance(0, 11, [describe_source(source)], "d" * 64)
        state = writer.checkpoint()
        writer.finish("e" * 64)
    with HistoryReader(path, state) as reader:
        assert reader.unchanged()
        assert [r["op"] for r in reader.after(None)] == ["sync", "advance"]
    with pytest.raises(ValueError, match="tail"):
        HistoryWriter.restore(path, state)
    before = path.read_bytes()
    with HistoryWriter.restore(path, state, branch_path=tmp_path / "branch.jsonl") as writer:
        writer.finish("e" * 64)
        assert writer.checkpoint()["closed"]
    assert path.read_bytes() == before


def test_history_rejects_gaps_and_bad_source_coverage(tmp_path):
    from zlscan.io.producer_history import HistoryWriter

    with HistoryWriter(tmp_path / "history.jsonl", header()) as writer:
        with pytest.raises(ValueError, match="coverage|source"):
            writer.advance(0, 11, [], "d" * 64)
        assert writer.step == 0
