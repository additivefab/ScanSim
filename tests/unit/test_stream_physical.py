import json
from dataclasses import replace

import numpy as np

from zlscan.cli import main
from zlscan.config.presets import demo_config
from zlscan.contracts.flags import InvalidFlag
from zlscan.engine.physical_run import stream_raw
from zlscan.engine.stream_physical import stream_physical
from zlscan.io.rawstream import read_all, scan_blocks
from zlscan.transport.queues import StageSpec
from zlscan.truth.scene import uniform


def _configurations():
    a = demo_config()
    a.detector = replace(a.detector, shot_noise=False)
    b = replace(a, scenes={"main": uniform(1.2, a.scan.width_m, 1)})
    return a, b


def test_two_streamed_channels_match_independent_analogue_runs(tmp_path):
    a, b = _configurations()
    expected = []
    for index, cfg in enumerate((a, b)):
        path = tmp_path / f"{index}.zlraw"
        stream_raw(path, cfg, 4)
        expected.append(read_all(path))
    out = tmp_path / "dual"
    result = stream_physical(out, a, b, 4)
    assert result["integrity"] == "complete"
    image = out / "observable"
    streamed = []

    def consume(session, block, codes):
        assert block.channels == [0, 1]
        assert session.channel_roles == {"0": None, "1": None}
        streamed.append(codes.copy())

    scan = scan_blocks(image / "raw.zlraw", consume)
    assert scan.segments == []
    assert not scan.errors
    for index, block in enumerate(scan.blocks):
        assert block.first_sample_index == expected[0].blocks[index].first_sample_index
        for channel, old in enumerate(expected):
            np.testing.assert_array_equal(
                streamed[index][:, channel], old.segments[index].samples[:, 0]
            )
    assert np.any(streamed[0][:, 0] != streamed[0][:, 1])
    reconstructed = np.fromfile(image / "codes_u16le.bin", dtype="<u2")
    np.testing.assert_array_equal(reconstructed, np.asarray(streamed).ravel())


def test_dropped_dual_packet_leaves_a_row_and_keeps_following_line_identity(tmp_path):
    a, b = _configurations()
    out = tmp_path / "loss"
    result = stream_physical(
        out,
        a,
        b,
        4,
        stages=[StageSpec("disk", 10**6, 10**9, fail_windows=((3 * 10**9, 2 * 10**9),))],
    )
    assert result["integrity"] == "incomplete"
    obs = out / "observable"
    losses = json.loads((obs / "losses.json").read_text())
    assert len(losses) == 1 and losses[0]["channels"] == [0, 1]
    assert losses[0]["item_id"] == 1 and losses[0]["reason"] == "write_failed"
    scan = scan_blocks(obs / "raw.zlraw", lambda *_: None)
    assert [b.line_number for b in scan.blocks] == [0, 2, 3]
    mask = np.fromfile(obs / "invalid_u16le.bin", dtype="<u2").reshape(4, -1)
    assert (mask[1] == int(InvalidFlag.MISSING_SAMPLE)).all()
    assert not mask[[0, 2, 3]].any()


def test_physical_cli_streams_two_channels(tmp_path):
    out = tmp_path / "cli"
    assert main(["run", "--mode", "physical", "--stream", "--lines", "2", "--out", str(out)]) == 0
    assert json.loads((out / "run.json").read_text())["channels"] == [0, 1]
