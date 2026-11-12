import shutil
import struct

import numpy as np
import pytest

from zlscan.digital.cosim import run_capture_cosim


@pytest.mark.skipif(
    not (shutil.which("iverilog") and shutil.which("vvp")),
    reason="Icarus Verilog and vvp are unavailable",
)
def test_icarus_matches_capture_window_reference():
    result = run_capture_cosim()
    assert result["passed"]
    assert set(result["cases"]) == {"ramp", "alternating", "prbs"}
    assert all(
        case["samples"] == 15 and case["ignored_syncs"] == [1] for case in result["cases"].values()
    )
    assert result["sample_index_bits"] == 64
    for case in result["counter_boundary_cases"].values():
        indices = [record[0] for record in case["records"]]
        assert min(indices) < 2**32 < max(indices)
        assert indices == sorted(indices)
        assert case["match"] and case["ignored_syncs"] == [1]
    reset = result["counter_reset"]
    assert reset["passed"]
    before = [row for row in reset["records"] if row[0] == 0]
    after = [row for row in reset["records"] if row[0] == 1]
    assert before and after and min(r[1] for r in before) < 2**32 < max(r[1] for r in before)
    assert after[0][1] < 20 and after[0][2] == 0


@pytest.mark.skipif(
    not (shutil.which("iverilog") and shutil.which("vvp")), reason="Icarus unavailable"
)
def test_real_rtl_high_bits_survive_packet_loss_reconnect_and_new_epoch(tmp_path):
    from zlscan.adc.patterns import ConvertedSamples
    from zlscan.contracts.time import ClockDomain
    from zlscan.digital.acquisition import AcquisitionConfig, session_header
    from zlscan.io.rawstream import (
        FLAG_EPOCH_START,
        FLAG_LINE_END,
        FLAG_LINE_START,
        RawWriter,
        read_all,
    )

    result = run_capture_cosim()
    records = result["counter_boundary_cases"]["ramp"]["records"]
    source = ConvertedSamples(
        np.zeros((1, 1), np.uint16), (0,), 0, 0, ClockDomain(1, 1_000_000_000), 16
    )
    header = session_header(source, AcquisitionConfig(2, 5), 1)
    path = tmp_path / "boundary.zlraw"
    with RawWriter(path, header) as writer:
        for line in range(3):
            points = [r for r in records if r[1] == line]
            writer.write_block(
                epoch=0,
                channel_mask=1,
                clock_domain=1,
                first_sample_index=points[0][0],
                samples=np.array([[p[2]] for p in points], np.uint16),
                line_number=line,
                flags=FLAG_LINE_START | FLAG_LINE_END,
            )
        reset = [r for r in result["counter_reset"]["records"] if r[0] == 1 and r[2] == 0]
        writer.write_block(
            epoch=1,
            channel_mask=1,
            clock_domain=1,
            first_sample_index=reset[0][1],
            samples=np.array([[p[3]] for p in reset], np.uint16),
            line_number=0,
            flags=FLAG_LINE_START | FLAG_LINE_END | FLAG_EPOCH_START,
        )
    # Lose the middle complete packet; the following packet retains its high source bits.
    data = path.read_bytes()
    first = 12 + struct.unpack_from("<I", data, 8)[0]
    block_size = 64 + 5 * 2
    path.write_bytes(data[: first + block_size] + data[first + 2 * block_size :])
    read = read_all(path)
    assert not read.fatal
    assert any(error.code == "block_sequence_gap" for error in read.errors)
    assert [(block.epoch, block.line_number) for block in read.blocks] == [(0, 0), (0, 2), (1, 0)]
    assert read.blocks[0].first_sample_index < 2**32 < read.blocks[1].first_sample_index
    assert read.blocks[2].first_sample_index < 20
    assert not any(error.code == "sample_index_regress" for error in read.errors)
