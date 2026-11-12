"""Persist original model flags at their native grids, with committed recovery semantics."""

import numpy as np
import pytest

from zlscan.contracts.time import ClockDomain


def header():
    from zlscan.io.model_quality import quality_header

    return quality_header(
        (0,),
        4,
        ClockDomain(2, 1_000_000),
        ClockDomain(2, 4_000_000),
        {"run_id": "r", "branch_id": "b", "session_id": 19},
        {"0": "a" * 64},
        "chain-0.3",
    )


def flags(start, end):
    fine = np.arange(start, end)
    adc = np.arange(-(-start // 4), -(-end // 4))
    return {
        0: {
            "adc_saturated": adc == 2,
            "front_end_clipped": fine == 7,
            "pmt_over_limit": (fine >= 9) & (fine < 13),
        }
    }


def test_native_grids_keep_between_adc_single_step_and_exact_epoch(tmp_path):
    from zlscan.io.model_quality import QualityReader, QualityWriter

    path = tmp_path / "quality.jsonl"
    with QualityWriter(path, header()) as writer:
        writer.append(0, 11, 2, flags(0, 11))
        writer.append(11, 17, 3, flags(11, 17))
        state = writer.checkpoint()
    before = path.read_bytes()
    with QualityReader(path, state) as reader:
        result = reader.query(0, 0, 17, None)
        assert [r["step"] for r in result["events"]] == [7, 8, 9, 10, 11, 12]
        assert [r["epoch"] for r in result["events"]] == [2, 2, 2, 2, 3, 3]
        assert result["events"][0]["sample_grid"] == "fine"
        assert result["events"][1]["source_sample_index"] == "2"
        assert result["events"][0]["source_sample_index"] is None
        assert all(r["visibility"] == "ORACLE_ONLY" for r in result["events"])
        assert reader.query(0, 7, 8, {"front_end_clipped"})["events"][0]["step_exact"] == "7"
        assert reader.query(0, 1, 3, {"adc_saturated"})["events"] == []
    assert path.read_bytes() == before


def test_quality_resume_branch_and_unknown_tail_preserve_source(tmp_path):
    from zlscan.io.model_quality import QualityReader, QualityWriter

    path = tmp_path / "quality.jsonl"
    with QualityWriter(path, header()) as writer:
        writer.append(0, 11, 2, flags(0, 11))
        state = writer.checkpoint()
    with QualityWriter.restore(path, state) as writer:
        writer.append(11, 17, 3, flags(11, 17))
        final = writer.checkpoint()
    with path.open("ab") as stream:
        stream.write(b"unknown tail")
    with QualityReader(path, final) as reader:
        assert reader.unchanged()
        assert len(reader.query(0, 0, 17, None)["events"]) == 6
    with pytest.raises(ValueError, match="tail"):
        QualityWriter.restore(path, final)
    before = path.read_bytes()
    branch = tmp_path / "branch.jsonl"
    with QualityWriter.restore(path, final, branch_path=branch) as writer:
        assert writer.step == 17
    assert path.read_bytes() == before and branch.read_bytes() == before[:-12]


def test_event_budget_is_reported_and_committed_mutation_is_refused(tmp_path):
    from zlscan.io.model_quality import QualityReader, QualityWriter

    path = tmp_path / "quality.jsonl"
    with QualityWriter(path, header()) as writer:
        dense = flags(0, 100)
        for values in dense[0].values():
            values[:] = True
        writer.append(0, 100, 0, dense)
        state = writer.checkpoint()
    with QualityReader(path, state) as reader:
        result = reader.query(0, 0, 100, None, limit=5)
        assert result["truncated"] and len(result["events"]) == 5
        body = path.read_bytes()
        path.write_bytes(body[:-2] + b"!\n")
        with pytest.raises(ValueError, match="SHA"):
            reader.query(0, 0, 100, None)


def test_malformed_counts_cannot_commit_a_new_quality_boundary(tmp_path):
    from zlscan.io.model_quality import QualityWriter

    path = tmp_path / "quality.jsonl"
    with QualityWriter(path, header()) as writer:
        bad = flags(0, 17)
        bad[0]["front_end_clipped"] = np.zeros(4, dtype=bool)
        with pytest.raises(ValueError, match="shape|length|grid"):
            writer.append(0, 17, 0, bad)
        assert writer.step == 0


def test_quality_bounds_and_wrong_epoch_are_explicit(tmp_path):
    from zlscan.io.model_quality import QualityReader, QualityWriter

    path = tmp_path / "quality.jsonl"
    with QualityWriter(path, header()) as writer:
        writer.append(0, 17, 2, flags(0, 17))
        state = writer.checkpoint()
    with QualityReader(path, state) as reader:
        result = reader.query(0, -20, 9, {"front_end_clipped"})
        assert result["clipped_window"] and result["support_start"] == "0"
        assert result["requested_window"] == ["-20", "9"]
        assert reader.query(0, 20, 30, None)["status"] == "not_recorded"
        with pytest.raises(ValueError, match="epoch"):
            reader.check_epochs(lambda step: 3)


def test_bad_compressed_vector_is_bounded_and_rejected():
    import base64
    import zlib

    from zlscan.io.model_quality import _decode

    too_long = base64.b64encode(zlib.compress(b"x" * (1 << 20))).decode()
    with pytest.raises(ValueError, match="layout"):
        _decode(too_long, 1)
