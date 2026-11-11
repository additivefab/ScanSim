"""ZLRAW 0.1: what is written is read back bit for bit; damage is reported, never stitched."""

import json
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from zlscan.io.rawstream import (
    BLOCK_HEADER_SIZE,
    FLAG_EPOCH_START,
    FLAG_LINE_START,
    PRELUDE,
    RawWriter,
    SessionHeader,
    read_all,
    read_headerless_u16,
)

ROOT = Path(__file__).resolve().parents[2]


def session(**over):
    base = dict(
        schema_version="0.1", session_id=7, created_at="2026-11-11T00:00:00Z",
        hardware_hash=None, firmware_hash=None, wiring_rev=None, register_dump_hash=None,
        calibration_id=None, channel_roles={"0": "signal", "1": None},
        clock_domains=[{"id": 0, "nominal_hz": [20_000_000, 1], "description": "adc"}],
        coding="offset_binary", bits_per_sample=16, bytes_per_sample=2, origin="SIMULATED",
        windows={"digital_keep": "rows 0-3"},
    )  # fmt: skip
    base.update(over)
    return SessionHeader(**base)


def write(path, blocks, ses=None):
    ses = ses or session()
    with RawWriter(path, ses) as w:
        for kw in blocks:
            w.write_block(**kw)
    return path


def block(samples, first, epoch=0, mask=0b01, flags=0, line=0, domain=0):
    return dict(
        epoch=epoch, channel_mask=mask, clock_domain=domain, first_sample_index=first,
        samples=np.asarray(samples, dtype="<u2"), flags=flags, line_number=line,
    )  # fmt: skip


def rows(n, channels=1, start=0):
    return (np.arange(start, start + n * channels, dtype="<u2")).reshape(n, channels)


# ---- layout ---------------------------------------------------------------------------------


def test_block_header_is_64_bytes_with_the_documented_layout(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(5), first=100, flags=FLAG_LINE_START, line=3)])
    data = path.read_bytes()
    json_len = struct.unpack_from("<I", data, len(PRELUDE))[0]
    start = len(PRELUDE) + 4 + json_len
    h = data[start : start + BLOCK_HEADER_SIZE]
    assert BLOCK_HEADER_SIZE == 64 and h[:4] == b"ZLR1"
    version, header_len, session_id, epoch, seq, mask, domain, flags = struct.unpack_from(
        "<HHQIIHHH", h, 4
    )
    first, count, payload_bytes, line = struct.unpack_from("<QIII", h, 32)
    assert (version, header_len, session_id, epoch, seq) == (1, 64, 7, 0, 0)
    assert (mask, domain, flags) == (1, 0, FLAG_LINE_START)
    assert (first, count, payload_bytes, line) == (100, 5, 10, 3)
    assert struct.unpack_from("<I", h, 60)[0] == zlib.crc32(h[:60])
    assert struct.unpack_from("<I", h, 52)[0] == zlib.crc32(data[start + 64 : start + 64 + 10])


def test_the_session_header_is_json_and_declares_a_raw_code_domain(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(2), 0)])
    data = path.read_bytes()
    assert data.startswith(PRELUDE)
    n = struct.unpack_from("<I", data, len(PRELUDE))[0]
    meta = json.loads(data[len(PRELUDE) + 4 : len(PRELUDE) + 4 + n])
    assert meta["value_domain"] == "raw_code" and meta["session_id"] == 7


# ---- round trips ----------------------------------------------------------------------------


def test_single_channel_round_trip_is_bitwise(tmp_path):
    data = np.array([0, 1, 255, 256, 65535, 32768], dtype="<u2").reshape(-1, 1)
    result = read_all(write(tmp_path / "a.zlraw", [block(data, 0)]))
    assert result.errors == [] and result.losses == []
    (seg,) = result.segments
    assert np.array_equal(seg.samples, data) and seg.first_sample_index == 0


def test_two_channels_are_interleaved_and_come_back_in_order(tmp_path):
    data = rows(6, channels=2)
    result = read_all(write(tmp_path / "a.zlraw", [block(data, 10, mask=0b11)]))
    (seg,) = result.segments
    assert seg.channels == [0, 1] and np.array_equal(seg.samples, data)


def test_consecutive_blocks_form_one_continuous_run_without_loss(tmp_path):
    blocks = [block(rows(4, start=0), 0), block(rows(4, start=4), 4), block(rows(2, start=8), 8)]
    result = read_all(write(tmp_path / "a.zlraw", blocks))
    assert result.errors == [] and result.losses == []
    assert sum(len(s.samples) for s in result.segments) == 10


def test_the_session_header_round_trips(tmp_path):
    result = read_all(write(tmp_path / "a.zlraw", [block(rows(1), 0)]))
    assert result.session == session()


# ---- things that must be detected -----------------------------------------------------------


def first_block_offset(path):
    data = path.read_bytes()
    return len(PRELUDE) + 4 + struct.unpack_from("<I", data, len(PRELUDE))[0]


def corrupt(path, offset, xor=0xFF):
    data = bytearray(path.read_bytes())
    data[offset] ^= xor
    path.write_bytes(bytes(data))


def codes(result):
    return [e.code for e in result.errors]


def test_a_bad_file_magic_is_fatal(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(2), 0)])
    corrupt(path, 0)
    result = read_all(path)
    assert codes(result) == ["bad_file_magic"] and result.fatal and result.segments == []


def test_a_bad_block_magic_is_fatal(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(2), 0)])
    corrupt(path, first_block_offset(path))
    assert "bad_block_magic" in codes(read_all(path))


def test_an_unknown_block_version_is_fatal(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(2), 0)])
    off = first_block_offset(path) + 4
    data = bytearray(path.read_bytes())
    data[off : off + 2] = struct.pack("<H", 9)
    zlib_crc = zlib.crc32(bytes(data[first_block_offset(path) : first_block_offset(path) + 60]))
    data[first_block_offset(path) + 60 : first_block_offset(path) + 64] = struct.pack(
        "<I", zlib_crc
    )
    path.write_bytes(bytes(data))
    assert "unknown_version" in codes(read_all(path))


def test_a_corrupted_header_field_is_caught_by_the_header_crc(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(2), 0)])
    corrupt(path, first_block_offset(path) + 33)  # inside first_sample_index
    result = read_all(path)
    assert "header_crc" in codes(result) and result.fatal and result.segments == []


def test_a_corrupted_payload_becomes_an_exact_loss_not_wrong_data(tmp_path):
    blocks = [block(rows(4, start=0), 0), block(rows(4, start=4), 4), block(rows(4, start=8), 8)]
    path = write(tmp_path / "a.zlraw", blocks)
    second = first_block_offset(path) + 64 + 8  # skip block 0 (header + 4 samples * 2 bytes)
    corrupt(path, second + 64 + 2)
    result = read_all(path)
    assert "payload_crc" in codes(result)
    assert [
        (loss.first_sample_index, loss.last_sample_index, loss.certainty) for loss in result.losses
    ] == [(4, 7, "exact")]
    kept = np.concatenate([s.samples for s in result.segments]).ravel().tolist()
    assert kept == list(range(0, 4)) + list(range(8, 12)), "the damaged block must not be used"


def test_a_missing_block_is_a_loss_with_the_exact_sample_range(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(4), 0), block(rows(4), 4), block(rows(4), 8)])
    data = path.read_bytes()
    start = first_block_offset(path)
    size = 64 + 8
    path.write_bytes(data[: start + size] + data[start + 2 * size :])  # cut the middle block
    result = read_all(path)
    assert "block_sequence_gap" in codes(result)
    assert [(loss.first_sample_index, loss.last_sample_index) for loss in result.losses] == [(4, 7)]


def test_a_repeated_or_backwards_block_sequence_is_rejected(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(4), 0), block(rows(4), 4)])
    data = path.read_bytes()
    start = first_block_offset(path)
    size = 64 + 8
    path.write_bytes(data[: start + size] + data[start : start + size] + data[start + size :])
    result = read_all(path)
    assert "sequence_regress" in codes(result)
    assert sum(len(s.samples) for s in result.segments) == 8, "the repeated block is not used twice"


def test_a_truncated_file_keeps_the_complete_blocks_and_reports_the_rest(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(4), 0), block(rows(4), 4)])
    path.write_bytes(path.read_bytes()[:-3])
    result = read_all(path)
    assert "truncated" in codes(result)
    assert sum(len(s.samples) for s in result.segments) == 4


def test_an_epoch_change_needs_the_epoch_start_flag(tmp_path):
    bad = write(tmp_path / "bad.zlraw", [block(rows(2), 0, epoch=0), block(rows(2), 2, epoch=1)])
    assert "epoch_changed_without_flag" in codes(read_all(bad))
    good = write(
        tmp_path / "good.zlraw",
        [block(rows(2), 0, epoch=0), block(rows(2), 2, epoch=1, flags=FLAG_EPOCH_START)],
    )
    assert read_all(good).errors == []


def test_a_block_from_another_session_is_rejected(tmp_path):
    path = write(tmp_path / "a.zlraw", [block(rows(2), 0)])
    # rewrite the session id in the block header and fix its CRC
    off = first_block_offset(path)
    data = bytearray(path.read_bytes())
    data[off + 8 : off + 16] = struct.pack("<Q", 99)
    data[off + 60 : off + 64] = struct.pack("<I", zlib.crc32(bytes(data[off : off + 60])))
    path.write_bytes(bytes(data))
    assert "session_mismatch" in codes(read_all(path))


def test_nothing_is_silently_dropped_or_added(tmp_path):
    """Whatever the damage, produced = delivered + lost (counted per sample)."""
    path = write(tmp_path / "a.zlraw", [block(rows(4), 0), block(rows(4), 4), block(rows(4), 8)])
    corrupt(path, first_block_offset(path) + 72 + 64 + 1)
    result = read_all(path)
    delivered = sum(len(s.samples) for s in result.segments)
    lost = sum(loss.last_sample_index - loss.first_sample_index + 1 for loss in result.losses)
    assert delivered + lost == 12


# ---- the writer refuses nonsense ------------------------------------------------------------


def test_writer_rejects_a_channel_count_that_disagrees_with_the_mask(tmp_path):
    with (
        RawWriter(tmp_path / "a.zlraw", session()) as w,
        pytest.raises(ValueError, match="channel"),
    ):
        w.write_block(**block(rows(3, channels=2), 0, mask=0b01))


def test_writer_rejects_values_outside_the_declared_bit_width(tmp_path):
    ses = session(bits_per_sample=12)
    with RawWriter(tmp_path / "a.zlraw", ses) as w, pytest.raises(ValueError, match="bits"):
        w.write_block(**block([[4096]], 0))


def test_writer_rejects_a_non_u16_array(tmp_path):
    with RawWriter(tmp_path / "a.zlraw", session()) as w, pytest.raises(ValueError, match="uint16"):
        w.write_block(epoch=0, channel_mask=1, clock_domain=0, first_sample_index=0,
                      samples=np.zeros((2, 1), dtype=np.float64))  # fmt: skip


def test_an_unknown_schema_version_is_refused(tmp_path):
    with pytest.raises(ValueError, match="schema"):
        RawWriter(tmp_path / "a.zlraw", session(schema_version="9.9"))


# ---- package B's headerless format ----------------------------------------------------------


B_BIN = ROOT / "legacy/package_b/examples/ad9268_50m_5x/raw_roi_u16le.bin"


def test_package_b_binary_is_read_with_explicit_dimensions():
    image = read_headerless_u16(B_BIN, nx=256, ny=32, samples_per_pixel=5, coding="offset_binary")
    assert image.shape == (32, 256 * 5) and image.dtype == np.dtype("<u2")
    assert np.array_equal(image.ravel(), np.fromfile(B_BIN, dtype="<u2"))


def test_a_size_mismatch_is_an_error_never_padded_or_cropped():
    with pytest.raises(ValueError, match="expected"):
        read_headerless_u16(B_BIN, nx=256, ny=33, samples_per_pixel=5, coding="offset_binary")


def test_twos_complement_words_are_converted_to_offset_binary(tmp_path):
    raw = np.array([0x0000, 0x7FFF, 0x8000, 0xFFFF], dtype="<u2")
    path = tmp_path / "twos.bin"
    raw.tofile(path)
    out = read_headerless_u16(path, nx=4, ny=1, samples_per_pixel=1, coding="twos_complement")
    assert out.ravel().tolist() == [0x8000, 0xFFFF, 0x0000, 0x7FFF]


def test_the_coding_must_be_stated():
    with pytest.raises(ValueError, match="coding"):
        read_headerless_u16(B_BIN, nx=256, ny=32, samples_per_pixel=5, coding="whatever")
