"""Reconstruct a delivered ZLRAW file or explicit headerless ROI using instrument geometry."""

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.contracts.calibration import CalibrationHeader, CaptureDescription
from zlscan.contracts.flags import InvalidFlag
from zlscan.io.facet_timing import FacetTiming, GeometryModel, observed_phases
from zlscan.io.line_image import reconstruct
from zlscan.io.manifest import sha256_file
from zlscan.io.quality import apply_quality_log
from zlscan.io.rawstream import read_all, read_headerless_u16
from zlscan.recon.density import Grid, reconstruct_density


def reconstruct_capture(
    raw_path: Path,
    capture: CaptureDescription,
    calibration: CalibrationHeader,
    grid: Grid,
    output: Path,
    *,
    channel: int = 0,
    headerless_spp: int | None = None,
    coding: str = "offset_binary",
    quality_path: Path | None = None,
    facet_path: Path | None = None,
) -> dict:
    """Reconstruct using declared uniform coordinates and preserve missing line positions."""
    if sha256_file(raw_path) != capture.raw_sha256:
        raise ValueError("capture SHA-256 does not match its description")
    geometry = calibration.items.get("facet_geometry")
    if geometry is not None and geometry.status != "ok":
        geometry = None
    if (geometry is None) != (facet_path is None):
        raise ValueError("facet geometry and capture facet timing must be supplied together")
    offset_samples = 0.0
    delay = calibration.items.get("delay")
    if delay is not None and delay.status == "ok":
        if not isinstance(delay.value, dict) or type(delay.value.get("delay_ticks")) is not int:
            raise ValueError("calibration delay requires an integer delay_ticks")
    else:
        delay = None
    if headerless_spp is not None:
        if quality_path is not None or facet_path is not None:
            raise ValueError("quality and facet events require ZLRAW sample identities")
        if delay is not None:
            raise ValueError("headerless capture requires a clock declaration for calibrated delay")

        if headerless_spp <= 0 or channel != 0:
            raise ValueError("headerless input requires positive samples per pixel and channel 0")
        codes = read_headerless_u16(
            raw_path,
            nx=grid.width,
            ny=capture.lines,
            samples_per_pixel=headerless_spp,
            coding=coding,
        )
        flags = None
    else:
        raw = read_all(raw_path)
        session = raw.session
        if raw.fatal or session is None:
            raise ValueError("capture ZLRAW is unreadable")
        binding = capture.binding
        if (session.origin == "SIMULATED") != (binding.simulation_hash is not None):
            raise ValueError("capture origin and binding do not match")
        if session.channel_roles != binding.channel_roles:
            raise ValueError("capture channel roles do not match its description")
        if session.origin == "MEASURED":
            for field in ("hardware_hash", "wiring_rev", "firmware_hash", "register_dump_hash"):
                value = getattr(session, field)
                if value is None or value != getattr(binding, field):
                    raise ValueError(f"capture {field} does not match its description")
        first, last = binding.epoch_range
        if any(not first <= block.epoch <= last for block in raw.blocks):
            raise ValueError("capture contains a block outside the declared epoch range")
        if any(not 0 <= block.line_number < capture.lines for block in raw.blocks):
            raise ValueError("capture contains a block outside the declared line range")
        image = reconstruct(raw, range(capture.lines))
        if channel not in image.codes:
            raise ValueError(f"capture lacks channel {channel}")
        codes, flags = image.codes[channel], image.invalid
        if quality_path is not None:
            apply_quality_log(quality_path, capture.raw_sha256, raw, channel, flags)
        if delay is not None and geometry is None:
            domains = {block.clock_domain for block in raw.blocks if channel in block.channels}
            clocks = [clock for clock in session.clock_domains if clock["id"] in domains]
            if len(clocks) != 1:
                raise ValueError("delay compensation requires one observed sample clock")
            clock = clocks[0]
            offset_samples = (
                delay.value["delay_ticks"] * clock["freq_num"] / (1e12 * clock["freq_den"])
            )

    cols = codes.shape[1]
    width_m = grid.width * grid.pitch_x_m
    height_m = grid.height * grid.pitch_y_m
    x = grid.origin_x_m + (np.arange(cols) + 0.5 - offset_samples) / cols * width_m
    y = grid.origin_y_m + (np.arange(capture.lines) + 0.5) / capture.lines * height_m
    coordinate_source = "declared uniform capture coordinates"
    x, y = np.broadcast_to(x, codes.shape), np.broadcast_to(y[:, None], codes.shape)
    if geometry is not None:
        timing = FacetTiming.model_validate_json(facet_path.read_bytes())
        model = GeometryModel.model_validate(geometry.value)
        if timing.policy != model.policy or session.windows != model.window:
            raise ValueError("capture facet policy or sample window does not match its calibration")
        phases = observed_phases(timing, raw, capture.raw_sha256, capture.lines)
        coordinate_source = "observable facet timing and calibrated target coordinates"
        x, y = x.copy(), y.copy()
        u = 2 * (np.arange(cols) + 0.5) / cols - 1
        for row, phase in enumerate(phases):
            fit = model.facets.get(str(phase))
            if fit is None:
                flags[row] |= int(InvalidFlag.FACET_UNRESOLVED)
                continue
            a, b, c = fit.x_coefficients_m
            lo, hi = fit.support_u
            x[row] = np.where((u >= lo) & (u <= hi), a + b * u + c * u**3, np.nan)
            if fit.y_offset_m is not None:
                y[row] += fit.y_offset_m
    result = reconstruct_density(
        codes,
        x,
        y,
        grid,
        calibration,
        capture.binding,
        channel=channel,
        sample_flags=flags,
    )
    output.mkdir(parents=True)
    np.savez(
        output / "density.npz",
        density=result.density,
        transmittance=result.transmittance,
        flags=result.flags,
        support_weight=result.support_weight,
    )
    manifest = {
        "mode": "reconstruct",
        "algorithm": "bilinear-linear-domain-v1",
        "domain": "density",
        "unit": "OD",
        "calibration_id": result.calibration_id,
        "calibration_provenance": calibration.provenance.value,
        "coordinate_source": coordinate_source,
        "delay_offset_samples": offset_samples,
        "support": "positive bilinear weight within one grid pitch per axis",
        "minimum_support_weight": 0.5,
        "valid_fraction": result.valid_fraction,
        "grid": asdict(grid),
        "raw_sha256": capture.raw_sha256,
    }
    if facet_path is not None:
        manifest["facet_timing_sha256"] = sha256_file(facet_path)
    if quality_path is not None:
        manifest["quality_sha256"] = sha256_file(quality_path)
    (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
