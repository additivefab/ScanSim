"""Persistent research probes preserve statistics, bounded views and recovery prefixes."""

import numpy as np
import pytest


def policy(raw=False):
    from zlscan.io.probe_store import TracePolicy

    return TracePolicy(probes=("power_w",), base=8, factor=4, keep_raw=raw)


def append(writer, data, start=0):
    writer.append(start, {0: {"power_w": np.asarray(data, dtype=float)}})


def test_streamed_summary_retains_a_one_step_spike_and_pending_tail(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter

    data = np.zeros(103)
    data[49] = 71
    with TraceWriter(tmp_path / "trace", policy(), (0,)) as writer:
        for start in range(0, len(data), 7):
            append(writer, data[start : start + 7], start)
        state = writer.checkpoint()
    with TraceReader(tmp_path / "trace", state) as reader:
        query = reader.query(0, "power_w", 0, 103, 2)
        assert len(query["values"]) <= 2
        assert max(row[1] for row in query["values"]) == 71
        assert sum(row[4] for row in query["values"]) == 103
        assert query["visibility"] == "ORACLE_ONLY"
        assert reader.query(0, "power_w", 47, 51, 20)["status"] == "not_recorded"


def test_raw_window_is_exact_and_output_bounded(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter

    values = np.arange(129, dtype=float)
    with TraceWriter(tmp_path / "trace", policy(True), (0,)) as writer:
        append(writer, values[:53])
        append(writer, values[53:], 53)
        state = writer.checkpoint()
    with TraceReader(tmp_path / "trace", state) as reader:
        assert reader.query(0, "power_w", 47, 59, 20)["values"] == values[47:59].tolist()
        coarse = reader.query(0, "power_w", 1, 128, 3)
        assert len(coarse["values"]) <= 3
        assert coarse["values"][0][0] == 1 and coarse["values"][-1][1] == 127


def test_resume_keeps_pending_buckets_and_refuses_unknown_tail(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter

    folder = tmp_path / "trace"
    with TraceWriter(folder, policy(), (0,)) as writer:
        append(writer, np.arange(19))
        state = writer.checkpoint()
    with TraceWriter.restore(folder, state) as writer:
        append(writer, np.arange(19, 65), 19)
        final = writer.checkpoint()
    with TraceReader(folder, final) as reader:
        rows = reader.query(0, "power_w", 0, 65, 2)["values"]
        assert rows[0][:4] == [0, 63, 0, 63] and rows[1][:4] == [64, 64, 64, 64]
    with (folder / "index.jsonl").open("ab") as stream:
        stream.write(b"uncommitted")
    with pytest.raises(ValueError, match="tail"):
        TraceWriter.restore(folder, final)
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with TraceWriter.restore(folder, final, branch_directory=tmp_path / "branch") as writer:
        assert writer.step == 65
    assert before == {p.name: p.read_bytes() for p in folder.iterdir()}


def test_orphan_shard_is_not_silently_reused(tmp_path):
    from zlscan.io.probe_store import TraceWriter

    folder = tmp_path / "trace"
    with TraceWriter(folder, policy(), (0,)) as writer:
        append(writer, np.arange(19))
        state = writer.checkpoint()
        append(writer, np.arange(19, 21), 19)
    with pytest.raises(ValueError, match="tail|orphan"):
        TraceWriter.restore(folder, state)
    with TraceWriter.restore(folder, state, branch_directory=tmp_path / "branch") as writer:
        append(writer, [19, 20], 19)
        assert writer.step == 21


def test_committed_shard_mutation_and_bad_pending_are_refused(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter

    with TraceWriter(tmp_path / "trace", policy(), (0,)) as writer:
        append(writer, np.arange(19))
        state = writer.checkpoint()
    shard = next((tmp_path / "trace").glob("*.h5"))
    shard.write_bytes(shard.read_bytes() + b"mutation")
    with pytest.raises(ValueError, match="SHA"):
        TraceReader(tmp_path / "trace", state)


def test_probe_selection_rejects_unknowns_before_creating_output(tmp_path):
    from zlscan.io.probe_store import TracePolicy

    with pytest.raises(ValueError):
        TracePolicy(probes=("invented",))


def test_pending_geometry_is_validated_even_with_a_fresh_checksum(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter, _digest

    with TraceWriter(tmp_path / "trace", policy(), (0,)) as writer:
        append(writer, np.arange(19))
        state = writer.checkpoint()
    state["pending"]["0/power_w"][0][1] = 4
    state.pop("checksum")
    state["checksum"] = _digest(state)
    with pytest.raises(ValueError, match="pending"):
        TraceReader(tmp_path / "trace", state)


def test_summary_edges_report_actual_support_across_late_completed_shards(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter

    with TraceWriter(tmp_path / "trace", policy(), (0,)) as writer:
        for first in range(0, 103, 3):
            append(writer, np.arange(first, min(first + 3, 103)), first)
        state = writer.checkpoint()
    with TraceReader(tmp_path / "trace", state) as reader:
        result = reader.query(0, "power_w", 7, 70, 1)
        assert result["boundary_extra"]
        assert result["support_start"] == 0 and result["support_end"] == 96
        assert result["values"] == [[0, 95, 0, 95, 96]]


def test_recorded_summary_is_independent_of_producer_chunk_boundaries(tmp_path):
    from zlscan.io.probe_store import TraceReader, TraceWriter

    values = np.sin(np.arange(313, dtype=float))
    values[111] = 30
    results = []
    for chunk in [7, 53, 313]:
        folder = tmp_path / str(chunk)
        with TraceWriter(folder, policy(), (0,)) as writer:
            for first in range(0, len(values), chunk):
                append(writer, values[first : first + chunk], first)
            state = writer.checkpoint()
        with TraceReader(folder, state) as reader:
            results.append(reader.query(0, "power_w", 5, 310, 3))
    assert results[0] == results[1] == results[2]
