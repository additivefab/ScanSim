"""Queue graph, integrity report and minimal reconstruction (S2-T04, S2-T05)."""

import numpy as np
import pytest

from zlscan.adc import patterns
from zlscan.budget.modes import Stage, simulate_stage_backlogs
from zlscan.contracts.flags import InvalidFlag
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, acquire, write_raw
from zlscan.io.integrity import check_file, check_result
from zlscan.io.rawstream import RawWriter, read_all
from zlscan.recon.minimal import reconstruct
from zlscan.transport.queues import Item, StageSpec, finish_time, simulate

MS = 10**9  # picoseconds


def stream(n, size=1000, gap_ps=MS // 10, count=10):
    return [Item(i, size, 0, 1, i * count, count, i * gap_ps, i) for i in range(n)]


# ---- queues ---------------------------------------------------------------------------------


def test_finish_time_pauses_during_stalls_and_never_banks_idle_time():
    assert finish_time(0, 100, ()) == 100
    assert finish_time(0, 100, ((50, 30),)) == 130
    assert finish_time(60, 100, ((50, 30),)) == 180  # waits for the stall to end, then 100


def test_fast_stages_deliver_everything_in_order_with_nothing_lost():
    items = stream(50)
    r = simulate(items, [StageSpec("rx", 10_000, 1e9), StageSpec("disk", 10_000, 1e9)])
    assert [i.item_id for i, _ in r.delivered] == list(range(50))
    assert r.losses == [] and r.conserved(items)


def test_a_full_first_stage_loses_the_arriving_block_with_its_exact_sample_range():
    items = stream(20, gap_ps=MS // 100)
    r = simulate(items, [StageSpec("rx", 3000, 1e5)])  # far too slow, room for 3 blocks
    assert r.losses and all(x.layer == "rx" and x.reason == "overflow" for x in r.losses)
    lost = r.losses[0]
    item = items[lost.item_id]
    assert (lost.first_sample_index, lost.last_sample_index) == (
        item.first_sample_index,
        item.first_sample_index + item.sample_count - 1,
    )
    assert r.conserved(items)


def test_a_later_stage_pushes_back_instead_of_dropping():
    items = stream(10, gap_ps=MS // 1000)
    stages = [StageSpec("rx", 20_000, 1e9), StageSpec("disk", 2000, 1e6, backpressure=True)]
    r = simulate(items, stages)
    assert r.stats[0].peak_bytes > 2000  # the backlog sits in the first stage
    assert not any(x.layer == "disk" for x in r.losses)
    assert r.conserved(items)


def test_a_stage_without_backpressure_drops_and_says_which_layer():
    items = stream(10, gap_ps=MS // 1000)
    stages = [StageSpec("rx", 20_000, 1e9), StageSpec("dma", 2000, 1e6, backpressure=False)]
    r = simulate(items, stages)
    assert any(x.layer == "dma" and x.reason == "overflow" for x in r.losses)
    assert r.conserved(items)


def test_a_dead_link_for_a_window_shows_up_as_losses_in_that_layer_only():
    items = stream(100, gap_ps=MS // 10)
    stages = [StageSpec("rx", 5_000, 1e9), StageSpec("net", 5_000, 1e9, stalls=((2 * MS, 10**15),))]
    r = simulate(items, stages, end_ps=20 * MS)
    assert r.losses and {x.layer for x in r.losses} == {"rx"}
    assert r.conserved(items)


def test_a_host_write_failure_is_a_loss_at_the_last_layer_with_a_range():
    items = stream(10)
    stages = [
        StageSpec("rx", 10_000, 1e9),
        StageSpec("disk", 10_000, 1e9, fail_windows=((2 * MS // 10, MS // 5),)),
    ]
    r = simulate(items, stages)
    bad = [x for x in r.losses if x.layer == "disk" and x.reason == "write_failed"]
    assert bad
    assert r.conserved(items)


def test_unknown_rates_are_refused_not_assumed():
    with pytest.raises(ValueError, match="nothing is assumed"):
        simulate(stream(3), [StageSpec("rx", 1000, None)])


def test_peak_backlog_agrees_with_the_fluid_budget_model():
    """Same arrival and service curves as the stage-1 budget: 200 MB/s in, 100 MB/s out, 0.1 s."""
    size, rate_in = 20_000, 200e6
    gap_ps = int(size / rate_in * 1e12)
    n = int(0.1 * rate_in / size)
    items = [Item(i, size, 0, 1, i, 1, i * gap_ps) for i in range(n)]
    r = simulate(items, [StageSpec("ddr", 40_000_000, 100e6)])
    fluid = simulate_stage_backlogs(200e6, 0.1, [Stage("ddr", 40e6, 100e6)], horizon_s=0.5)
    assert r.stats[0].peak_bytes == pytest.approx(fluid[0].peak_backlog_bytes, rel=0.03)


# ---- integrity + reconstruction -------------------------------------------------------------

DOM = ClockDomain(id=1, freq_num=50_000_000)
CFG = AcquisitionConfig(window_start=10, window_length=40)
TICKS = 200 * 20_000


def build(tmp_path, drop=(), lines=6):
    src = patterns.make_source({0: patterns.ramp(0, 1, 2000), 1: patterns.prbs(2, 2000)}, DOM)
    res = acquire(src, [7_000 + i * TICKS for i in range(lines)], CFG)
    res.lines = [x for x in res.lines if x.line_number not in drop]
    path = tmp_path / "a.zlraw"
    write_raw(path, src, res, CFG)
    return src, path


def test_a_clean_file_reconstructs_to_the_source_codes(tmp_path):
    src, path = build(tmp_path)
    img = reconstruct(path, range(0, 6))
    assert img.status == "complete" and not img.invalid.any()
    row = 3
    start = (
        3 * 200 + 2 + 10 + 0
    )  # sync at 7000 ps rounds up to cycle 1, plus 2 latency, plus window
    first = int(img.codes[0][row, 0])
    assert img.codes[0][row].tolist() == src.codes[first : first + 40, 0].tolist()
    assert img.codes[1][row].tolist() == src.codes[first : first + 40, 1].tolist()
    assert start > 0


def test_a_missing_line_stays_invalid_and_later_data_does_not_move_up(tmp_path):
    _, path = build(tmp_path, drop={2})
    img = reconstruct(path, range(0, 6))
    assert img.status == "incomplete" and img.missing_lines == [2]
    assert (img.invalid[2] == int(InvalidFlag.MISSING_SAMPLE)).all()
    assert not img.invalid[3].any()
    rep = check_file(path, range(0, 6))
    assert rep.status == "incomplete" and rep.lines_missing == [2]
    # the writer never wrote line 2, so there is no sequence gap: only the line numbers show it
    assert check_file(path).lines_missing == [2]


def test_an_early_stop_reports_incomplete(tmp_path):
    _, path = build(tmp_path, lines=3)
    img = reconstruct(path, range(0, 6))
    assert img.status == "incomplete" and img.missing_lines == [3, 4, 5]


def test_a_corrupted_payload_is_reported_not_used(tmp_path):
    _, path = build(tmp_path)
    data = bytearray(path.read_bytes())
    data[-5] ^= 0xFF
    path.write_bytes(bytes(data))
    rep = check_file(path, range(0, 6))
    assert rep.status == "incomplete" and any("payload_crc" in e for e in rep.errors)
    img = reconstruct(path, range(0, 6))
    assert img.status == "incomplete"


def test_without_an_expected_range_the_report_says_what_it_cannot_know(tmp_path):
    _, path = build(tmp_path, lines=3)
    rep = check_result(read_all(path))
    assert rep.status == "complete" and any("no expected line range" in n for n in rep.notes)


def test_an_unreadable_file_is_not_called_complete(tmp_path):
    p = tmp_path / "bad.zlraw"
    p.write_bytes(b"junk")
    assert check_file(p).status == "unreadable"
    assert reconstruct(p, range(0, 2)).status == "incomplete"


def test_transport_losses_are_merged_into_the_report(tmp_path):
    _, path = build(tmp_path)
    extra = [
        {"layer": "net", "reason": "overflow", "first_sample_index": 1, "last_sample_index": 5}
    ]
    rep = check_result(read_all(path), range(0, 6), extra)
    assert rep.status == "incomplete" and rep.losses[-1]["source"] == "transport"
    assert np is not None and RawWriter is not None
