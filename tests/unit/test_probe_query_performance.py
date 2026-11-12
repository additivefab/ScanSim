"""Result-preserving query work bounds, independent of wall-clock speed."""

import json

import numpy as np
import pytest

from zlscan.io.probe_store import TracePolicy, TraceReader, TraceWriter


def create(tmp_path, raw, values=None):
    if values is None:
        values = np.sin(np.arange(4097, dtype=float))
    folder = tmp_path / "probes"
    with TraceWriter(
        folder, TracePolicy(probes=("power_w",), base=8, factor=4, keep_raw=raw), (0,)
    ) as writer:
        for first in range(0, len(values), 127):
            writer.append(first, {0: {"power_w": values[first : first + 127]}})
        state = writer.checkpoint()
    return folder, state


def test_raw_coarse_reduction_yields_groups_rather_than_each_sample(tmp_path, monkeypatch):
    folder, state = create(tmp_path, True)
    with TraceReader(folder, state) as reader:
        rows = reader._rows
        count = 0

        def counted(*args, **kwargs):
            nonlocal count
            for row in rows(*args, **kwargs):
                count += 1
                yield row

        monkeypatch.setattr(reader, "_rows", counted)
        result = reader.query(0, "power_w", 5, 4096, 3)
        assert len(result["values"]) == 3
        assert count <= len(list(folder.glob("*.h5"))) + 6


def test_summary_opens_only_contributing_tiers_but_retains_all_hash_checks(tmp_path, monkeypatch):
    import zlscan.io.probe_store as store

    folder, state = create(tmp_path, False)
    with TraceReader(folder, state) as reader:
        file_open, shard = store.h5py.File, store._shard
        opens, hashes = [], []

        def opened(*args, **kwargs):
            opens.append(args[0])
            return file_open(*args, **kwargs)

        def checked(*args):
            hashes.append(args[1]["sequence"])
            return shard(*args)

        monkeypatch.setattr(store.h5py, "File", opened)
        monkeypatch.setattr(store, "_shard", checked)
        result = reader.query(0, "power_w", 0, 4097, 2)
        assert len(result["values"]) == 2
        assert len(opens) == 2  # Two completed 2048-step buckets, plus the root's tail.
        assert len(hashes) == 2 * len(list(folder.glob("*.h5")))


def test_unused_tier_file_mutation_is_still_rejected_after_registration(tmp_path):
    folder, state = create(tmp_path, False)
    with TraceReader(folder, state) as reader:
        path = folder / "00000000000000000000.h5"
        path.write_bytes(path.read_bytes() + b"mutation")
        with pytest.raises(ValueError, match="SHA"):
            reader.query(0, "power_w", 0, 4097, 2)


@pytest.mark.parametrize("first_zero", [0.0, -0.0])
def test_raw_coarse_values_preserve_first_equal_extremum_signed_zero(tmp_path, first_zero):
    values = np.tile([first_zero, -first_zero], 257)
    folder, state = create(tmp_path, True, values)
    with TraceReader(folder, state) as reader:
        result = reader.query(0, "power_w", 3, 501, 3)
    block = -(-(501 - 3) // 3)
    expected = []
    for first in range(3, 501, block):
        part = values[first : min(first + block, 501)].tolist()
        expected.append([min(part), max(part), part[0], part[-1], len(part)])
    assert json.dumps(result["values"]) == json.dumps(expected)


def test_registration_rejects_wrong_header_even_when_shard_and_index_are_rehashed(tmp_path):
    import hashlib

    import h5py

    from zlscan.io.manifest import sha256_file
    from zlscan.io.probe_store import _digest

    folder, state = create(tmp_path, False)
    path = folder / "00000000000000000000.h5"
    with h5py.File(path, "r+") as file:
        file.attrs["start"] = "99"
    rows = [json.loads(line) for line in (folder / "index.jsonl").read_text().splitlines()]
    rows[1]["sha256"] = sha256_file(path)
    body = ("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)).encode()
    (folder / "index.jsonl").write_bytes(body)
    state["index"] = dict(offset=str(len(body)), prefix_sha256=hashlib.sha256(body).hexdigest())
    state.pop("checksum")
    state["checksum"] = _digest(state)
    with pytest.raises(ValueError, match="binding"):
        TraceReader(folder, state)
