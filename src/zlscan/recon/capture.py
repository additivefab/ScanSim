"""Reconstruct a delivered ZLRAW file or explicit headerless ROI using instrument geometry."""

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.contracts.calibration import CalibrationHeader, CaptureDescription
from zlscan.io.line_image import reconstruct
from zlscan.io.manifest import sha256_file
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
) -> dict:
    """Reconstruct using declared uniform coordinates and preserve missing line positions."""
    if sha256_file(raw_path) != capture.raw_sha256:
        raise ValueError("capture SHA-256 does not match its description")
    if headerless_spp is not None:
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

    cols = codes.shape[1]
    width_m = grid.width * grid.pitch_x_m
    height_m = grid.height * grid.pitch_y_m
    x = grid.origin_x_m + (np.arange(cols) + 0.5) / cols * width_m
    y = grid.origin_y_m + (np.arange(capture.lines) + 0.5) / capture.lines * height_m
    result = reconstruct_density(
        codes,
        np.broadcast_to(x, codes.shape),
        np.broadcast_to(y[:, None], codes.shape),
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
        "coordinate_source": "declared uniform capture coordinates",
        "support": "positive bilinear weight within one grid pitch per axis",
        "minimum_support_weight": 0.5,
        "valid_fraction": result.valid_fraction,
        "grid": asdict(grid),
        "raw_sha256": capture.raw_sha256,
    }
    (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
