"""Resume actual committed file prefixes, never silently rewrite or duplicate their tails."""

import json
import shutil

import numpy as np
import pytest

from zlscan.io.rawstream import RawWriter, SessionHeader, read_all


def session(session_id=19):
    return SessionHeader(
        schema_version="0.1",
        session_id=session_id,
        created_at="fixture",
        hardware_hash=None,
        firmware_hash=None,
        wiring_rev=None,
        register_dump_hash=None,
        calibration_id=None,
        channel_roles={"0": None},
        clock_domains=[{"id": 1, "freq_num": 1_000_000}],
        coding="offset_binary",
        bits_per_sample=16,
        bytes_per_sample=2,
        windows={},
        origin="SIMULATED",
    )


def block(writer, index):
    writer.write_block(
        epoch=0,
        channel_mask=1,
        clock_domain=1,
        first_sample_index=index * 4,
        samples=np.arange(index * 4, index * 4 + 4, dtype=np.uint16).reshape(-1, 1),
        line_number=index,
    )


def test_committed_prefix_resume_keeps_sequence_and_full_bytes(tmp_path):
    expected = tmp_path / "expected.zlraw"
    actual = tmp_path / "actual.zlraw"
    with RawWriter(expected, session()) as writer:
        for index in range(5):
            block(writer, index)
    with RawWriter(actual, session()) as writer:
        block(writer, 0)
        block(writer, 1)
        saved = writer.checkpoint()
        assert saved == writer.checkpoint()
    with RawWriter.resume(actual, session(), saved) as writer:
        assert writer.next_sequence == 2
        for index in range(2, 5):
            block(writer, index)
    assert actual.read_bytes() == expected.read_bytes()
    assert [b.seq for b in read_all(actual).blocks] == list(range(5))


def test_new_writer_does_not_overwrite_an_existing_file(tmp_path):
    path = tmp_path / "raw.zlraw"
    path.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        RawWriter(path, session())
    assert path.read_bytes() == b"keep"


def test_extra_tail_is_rejected_but_can_be_preserved_in_a_new_branch(tmp_path):
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session()) as writer:
        block(writer, 0)
        saved = writer.checkpoint()
        block(writer, 1)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="tail"):
        RawWriter.resume(path, session(), saved)
    assert path.read_bytes() == original
    branch = tmp_path / "branch.zlraw"
    with RawWriter.resume(path, session(), saved, branch_path=branch) as writer:
        block(writer, 1)
    assert path.read_bytes() == original and branch.read_bytes() == original
    # Reusing an already advanced checkpoint never appends duplicate rows.
    with pytest.raises(ValueError, match="tail"):
        RawWriter.resume(branch, session(), saved)


@pytest.mark.parametrize("damage", ["short", "same_size", "session", "checkpoint"])
def test_resume_rejects_wrong_prefix_or_state_without_writing(tmp_path, damage):
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session()) as writer:
        block(writer, 0)
        saved = writer.checkpoint()
    if damage == "short":
        path.write_bytes(path.read_bytes()[:-1])
    elif damage == "same_size":
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
    elif damage == "checkpoint":
        value = json.loads(saved)
        value["next_sequence"] = "99"
        saved = json.dumps(value).encode()
    before = path.read_bytes()
    with pytest.raises(ValueError):
        RawWriter.resume(path, session(20) if damage == "session" else session(), saved)
    assert path.read_bytes() == before


def test_live_writer_excludes_another_resume_handle(tmp_path):
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session()) as writer:
        block(writer, 0)
        saved = writer.checkpoint()
        with pytest.raises(OSError, match="lock"):
            RawWriter.resume(path, session(), saved)
    with RawWriter.resume(path, session(), saved) as writer:
        block(writer, 1)


def test_a_byte_identical_copy_can_be_resumed_at_another_path(tmp_path):
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session()) as writer:
        block(writer, 0)
        saved = writer.checkpoint()
    other = tmp_path / "copy.zlraw"
    shutil.copyfile(path, other)
    with RawWriter.resume(other, session(), saved) as writer:
        block(writer, 1)
    assert len(read_all(other).blocks) == 2 and len(read_all(path).blocks) == 1


def test_resume_checks_actual_block_sequence_even_with_rehashed_state(tmp_path):
    import hashlib

    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session()) as writer:
        block(writer, 0)
        value = json.loads(writer.checkpoint())
    value["next_sequence"] = "2"
    value.pop("checksum")
    value["checksum"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="sequence"):
        RawWriter.resume(path, session(), json.dumps(value).encode())
    assert path.read_bytes() == before


def test_external_edit_is_not_committed_by_a_live_writer(tmp_path):
    path = tmp_path / "raw.zlraw"
    with RawWriter(path, session()) as writer:
        block(writer, 0)
        writer.checkpoint()
        with path.open("r+b") as other:
            other.seek(-1, 2)
            other.write(b"X")
        with pytest.raises(ValueError, match="changed"):
            writer.checkpoint()
