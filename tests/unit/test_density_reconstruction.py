import numpy as np
import pytest

from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader
from zlscan.contracts.flags import InvalidFlag
from zlscan.recon.density import Grid, reconstruct_density


def calibration():
    return CalibrationHeader(
        format_version=1,
        provenance="SIMULATED",
        calibration_id="cal",
        created_at="2026-11-12T00:00:00Z",
        binds_to=Binding(simulation_hash="bench"),
        items={
            "transfer_lut": CalibItem(
                value={
                    "channels": {
                        "0": {
                            "code_knots": [0, 10, 20, 30],
                            "intensity_knots": [0.01, 0.1, 1.0, 10.0],
                            "white_outlier_columns": [],
                        }
                    },
                    "extrapolation": "invalid",
                }
            ),
            "dark_raw_code": CalibItem(value={"0": {"median": [0, 0]}}),
            "white_raw_code": CalibItem(value={"0": {"median": [20, 20]}}),
        },
    )


def test_linearized_samples_are_merged_before_density():
    cal = calibration()
    result = reconstruct_density(
        np.array([[10, 20]]),
        np.array([[0.5, 0.5]]),
        np.array([[0.5, 0.5]]),
        Grid(1, 1, 1, 1),
        cal,
        cal.binds_to,
        channel=0,
    )
    expected = ((0.1 - 0.01) / (1.0 - 0.01) + 1) / 2
    assert result.transmittance[0, 0] == pytest.approx(expected)
    assert result.density[0, 0] == pytest.approx(-np.log10(expected))
    assert not result.flags.any()


def test_invalid_values_keep_independent_reasons_and_no_finite_density():
    cal = calibration()
    result = reconstruct_density(
        np.array([[0, 31]]),
        np.array([[0.5, 1.5]]),
        np.array([[0.5, 0.5]]),
        Grid(3, 1, 1, 1),
        cal,
        cal.binds_to,
        channel=0,
        sample_flags=np.array([[0, InvalidFlag.ADC_SATURATED]], dtype=np.uint16),
    )
    assert np.isnan(result.density).all()
    assert result.flags[0, 0] & InvalidFlag.NONPOSITIVE_TRANSMITTANCE
    assert result.flags[0, 1] & InvalidFlag.OUTSIDE_CALIBRATION
    assert result.flags[0, 1] & InvalidFlag.ADC_SATURATED
    assert result.flags[0, 2] & InvalidFlag.RESAMPLING_SUPPORT


def test_incompatible_binding_is_refused_before_reconstruction():
    cal = calibration()
    with pytest.raises(ValueError, match="simulation_hash"):
        reconstruct_density(
            np.array([[10, 20]]),
            np.array([[0.5, 0.5]]),
            np.array([[0.5, 0.5]]),
            Grid(1, 1, 1, 1),
            cal,
            Binding(simulation_hash="different"),
            channel=0,
        )


def test_disk_splat_matches_batch_with_nonmonotone_rows(tmp_path):
    from zlscan.recon.disk_density import DiskDensity

    cal = calibration()
    grid = Grid(3, 4, 1, 1)
    codes = np.array([[10, 20], [10, 17], [21, 12], [20, 14]])
    x = np.array([[0.6, 1.2], [0.25, 1.4], [1.3, 2.6], [0.8, 2.4]])
    y = np.array([2.9, 0.7, 2.2, 1.3])
    flags = np.zeros_like(codes, dtype=np.uint16)
    flags[3, 0] = int(InvalidFlag.ADC_SATURATED)
    gains = np.array([1, 1.3, 0.8, 1])
    expected = reconstruct_density(
        codes,
        x,
        np.broadcast_to(y[:, None], codes.shape),
        grid,
        cal,
        cal.binds_to,
        channel=0,
        sample_flags=flags,
        row_gain=gains,
    )

    output = tmp_path / "output"
    with (tmp_path / "rows.bin").open("w+b") as scratch:
        disk = DiskDensity(scratch, grid, cal, 0, codes.shape[1])
        for index in range(len(y)):
            disk.add(codes[index], flags[index].copy(), x[index], y[index], gains[index])
        assert disk.write(tmp_path, output) == expected.valid_fraction
    with np.load(output / "density.npz") as actual:
        for name in actual.files:
            np.testing.assert_array_equal(actual[name], getattr(expected, name))


def test_headerless_reconstruction_streams_signed_words(tmp_path):
    from zlscan.contracts.calibration import CaptureDescription
    from zlscan.io.manifest import sha256_file
    from zlscan.recon.capture import reconstruct_capture

    cal = calibration()
    raw = tmp_path / "signed.bin"
    np.bitwise_xor(np.array([10, 20], dtype="<u2"), np.uint16(0x8000)).tofile(raw)
    capture = CaptureDescription(raw_sha256=sha256_file(raw), binding=cal.binds_to, lines=1)
    out = tmp_path / "image"
    summary = reconstruct_capture(
        raw,
        capture,
        cal,
        Grid(2, 1, 1, 1),
        out,
        headerless_spp=1,
        coding="twos_complement",
    )
    assert summary["storage"] == "row-streamed disk scratch"
    with np.load(out / "density.npz") as result:
        np.testing.assert_allclose(result["transmittance"], [[0.09 / 0.99, 1]])


def test_electronic_delay_is_only_applied_to_its_adc_channel(tmp_path):
    from zlscan.adc.patterns import ConvertedSamples
    from zlscan.contracts.calibration import CaptureDescription
    from zlscan.contracts.time import ClockDomain
    from zlscan.digital.acquisition import AcquisitionConfig, session_header
    from zlscan.io.manifest import sha256_file
    from zlscan.io.rawstream import FLAG_LINE_START, RawWriter
    from zlscan.recon.capture import reconstruct_capture

    cal = calibration()
    cal.binds_to.channel_roles = {"0": None, "1": None}
    for name in ("dark_raw_code", "white_raw_code"):
        cal.items[name].value["1"] = cal.items[name].value["0"]
    channels = cal.items["transfer_lut"].value["channels"]
    channels["1"] = channels["0"]
    cal.items["delay"] = CalibItem(value={"delay_ticks": 1_000_000, "channel": 0})
    raw = tmp_path / "dual.zlraw"
    src = ConvertedSamples(
        np.array([[10, 10], [20, 20]], dtype=np.uint16),
        (0, 1),
        0,
        0,
        ClockDomain(1, 1_000_000),
    )
    with RawWriter(raw, session_header(src, AcquisitionConfig(0, 2), 1)) as writer:
        writer.write_block(
            epoch=0,
            channel_mask=3,
            clock_domain=1,
            first_sample_index=0,
            samples=src.codes,
            flags=FLAG_LINE_START,
            line_number=0,
        )
    capture = CaptureDescription(raw_sha256=sha256_file(raw), binding=cal.binds_to, lines=1)
    for channel, offset in ((0, 1.0), (1, 0.0)):
        result = reconstruct_capture(
            raw,
            capture,
            cal,
            Grid(2, 1, 1, 1),
            tmp_path / f"channel-{channel}",
            channel=channel,
        )
        assert result["delay_offset_samples"] == offset
