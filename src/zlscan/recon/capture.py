"""Stream calibrated ZLRAW or explicit headerless ROI rows to a density archive."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.contracts.calibration import CalibrationHeader, CaptureDescription, require_applicable
from zlscan.contracts.flags import InvalidFlag
from zlscan.io.facet_timing import FacetTiming, GeometryModel, observed_phases
from zlscan.io.line_layout import line_layout, source_record
from zlscan.io.manifest import sha256_file
from zlscan.io.quality import quality_spans
from zlscan.io.rawstream import FLAG_LINE_START, BlockInfo, ReadResult, SessionHeader, scan_blocks
from zlscan.recon.density import Grid
from zlscan.recon.disk_density import DiskDensity


def _validate_capture(raw: ReadResult, capture: CaptureDescription) -> SessionHeader:
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
    return session


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
    """Process only observed sample rows and write the density and mask from disk scratch."""
    require_applicable(calibration, capture.binding)
    if sha256_file(raw_path) != capture.raw_sha256:
        raise ValueError("capture SHA-256 does not match its description")
    geometry = calibration.items.get("facet_geometry")
    model = (
        GeometryModel.model_validate(geometry.value)
        if geometry and geometry.status == "ok"
        else None
    )
    if (model is None) != (facet_path is None):
        raise ValueError("facet geometry and capture facet timing must be supplied together")
    delay = calibration.items.get("delay")
    delay_ticks = None
    if delay is not None and delay.status == "ok":
        if not isinstance(delay.value, dict) or type(delay.value.get("delay_ticks")) is not int:
            raise ValueError("calibration delay requires an integer delay_ticks")
        fitted_channel = delay.value.get("channel")
        if fitted_channel is None and len(capture.binding.channel_roles) > 1:
            raise ValueError("multi-channel capture requires channel-specific electronic delay")
        if fitted_channel is not None and type(fitted_channel) is not int:
            raise ValueError("invalid electronic delay channel")
        if fitted_channel is None or fitted_channel == channel:
            delay_ticks = delay.value["delay_ticks"]

    raw = None
    phases = None
    offset_samples = 0.0
    if headerless_spp is not None:
        if quality_path is not None or facet_path is not None:
            raise ValueError("quality and facet events require ZLRAW sample identities")
        if delay_ticks is not None:
            raise ValueError("headerless capture requires a clock declaration for calibrated delay")
        if headerless_spp <= 0 or channel != 0:
            raise ValueError("headerless input requires positive samples per pixel and channel 0")
        if coding not in {"offset_binary", "twos_complement"}:
            raise ValueError("coding must be 'offset_binary' or 'twos_complement'")
        cols = grid.width * headerless_spp
        if raw_path.stat().st_size != capture.lines * cols * 2:
            raise ValueError("headerless ROI byte count does not match the declared shape")
    else:
        raw = scan_blocks(raw_path, lambda *_: None)
        session = _validate_capture(raw, capture)
        cols = int(session.windows["window_length"])
        channel_blocks = [block for block in raw.blocks if channel in block.channels]
        if not channel_blocks:
            raise ValueError(f"capture lacks channel {channel}")
        if cols <= 0 or any(block.sample_count > cols for block in channel_blocks):
            raise ValueError("capture block is larger than its declared sample window")
        layouts = line_layout(raw.blocks, channel, cols)
        if delay_ticks is not None and model is None:
            domains = {block.clock_domain for block in channel_blocks}
            clocks = [clock for clock in session.clock_domains if clock["id"] in domains]
            if len(clocks) != 1:
                raise ValueError("delay compensation requires one observed sample clock")
            clock = clocks[0]
            offset_samples = delay_ticks * clock["freq_num"] / (1e12 * clock["freq_den"])
        if model is not None:
            timing = FacetTiming.model_validate_json(facet_path.read_bytes())
            if timing.policy != model.policy or session.windows != model.window:
                raise ValueError(
                    "capture facet policy or sample window does not match its calibration"
                )
            phases = observed_phases(timing, raw, capture.raw_sha256, capture.lines)

    width_m = grid.width * grid.pitch_x_m
    height_m = grid.height * grid.pitch_y_m
    columns = np.arange(cols) + 0.5
    nominal_x = grid.origin_x_m + (columns - offset_samples) / cols * width_m
    x_by_phase = {}
    gain_item = calibration.items.get("facet_gain")
    gains = (
        gain_item.value["channels"].get(str(channel), {})
        if model is not None and gain_item is not None and gain_item.status == "ok"
        else None
    )
    if model is not None:
        u = 2 * columns / cols - 1
        for phase, fit in model.facets.items():
            a, b, c = fit.x_coefficients_m
            lo, hi = fit.support_u
            x_by_phase[phase] = np.where((u >= lo) & (u <= hi), a + b * u + c * u**3, np.nan)

    # Save the actual coordinates used below, never an independently inferred display map.
    lineage_rows = []
    delivered = (
        {
            b.line_number: b
            for b in raw.blocks
            if channel in b.channels and b.flags & FLAG_LINE_START
        }
        if raw
        else {}
    )
    segmented = raw is not None and any(
        len(parts) > 1 or any(p.column_offset is None for p in parts) for parts in layouts.values()
    )

    with tempfile.TemporaryDirectory(prefix="zlscan_density_") as directory:
        scratch_dir = Path(directory)
        with (scratch_dir / "rows.bin").open("w+b") as scratch:
            accumulator = DiskDensity(scratch, grid, calibration, channel, cols)

            def add_row(line: int, codes: np.ndarray, flags: np.ndarray) -> None:
                y = grid.origin_y_m + (line + 0.5) / capture.lines * height_m
                x, gain = nominal_x, 1.0
                mapping = "uniform"
                if model is not None:
                    phase = str(phases[line])
                    fit = model.facets.get(phase)
                    if fit is None:
                        flags |= int(InvalidFlag.FACET_UNRESOLVED)
                    else:
                        x = x_by_phase[phase]
                        mapping = f"phase_{phase}"
                        y += fit.y_offset_m or 0
                        if gains is not None:
                            if phase in gains:
                                gain = gains[phase]
                            else:
                                flags |= int(InvalidFlag.WHITE_REFERENCE_INVALID)
                block = delivered.get(line)
                row_plan = {
                    "line": line,
                    "mapping": mapping,
                    "y_m": y,
                    "source": None if block is None else source_record(block),
                }
                if segmented:
                    row_plan.pop("source")
                    row_plan["segments"] = [
                        dict(
                            source=source_record(p.block),
                            column_offset=p.column_offset,
                            placement_reason=p.reason,
                        )
                        for p in layouts.get(line, [])
                    ]
                lineage_rows.append(row_plan)
                accumulator.add(codes, flags, x, y, gain)

            if raw is None:
                with raw_path.open("rb") as stream:
                    for line in range(capture.lines):
                        codes = np.frombuffer(stream.read(cols * 2), dtype="<u2")
                        if len(codes) != cols:
                            raise ValueError("headerless ROI ended before the declared row count")
                        if coding == "twos_complement":
                            codes = codes ^ np.uint16(0x8000)
                        add_row(line, codes, np.zeros(cols, dtype=np.uint16))
            else:
                spans = (
                    iter(())
                    if quality_path is None
                    else quality_spans(
                        quality_path, capture.raw_sha256, raw, channel, capture.lines, cols
                    )
                )
                span = next(spans, None)
                next_line = 0
                missing = int(InvalidFlag.MISSING_SAMPLE)
                parts_by_seq = {p.block.seq: p for parts in layouts.values() for p in parts}
                current_line = None
                row_codes = row_flags = None

                def add_missing(until: int) -> None:
                    nonlocal next_line
                    while next_line < until:
                        add_row(
                            next_line,
                            np.zeros(cols, dtype=np.uint16),
                            np.full(cols, missing, dtype=np.uint16),
                        )
                        next_line += 1

                def consume(_session: SessionHeader, block: BlockInfo, samples: np.ndarray) -> None:
                    nonlocal next_line, span, current_line, row_codes, row_flags
                    if channel not in block.channels:
                        return
                    if current_line is not None and block.line_number != current_line:
                        add_row(current_line, row_codes, row_flags)
                        next_line = current_line + 1
                    if (
                        not next_line <= block.line_number < capture.lines
                        or block.sample_count > cols
                    ):
                        raise ValueError("capture line layout changed while reconstructing")
                    if current_line != block.line_number:
                        add_missing(block.line_number)
                        current_line = block.line_number
                        row_codes = np.zeros(cols, dtype=np.uint16)
                        row_flags = np.full(cols, missing, dtype=np.uint16)
                    count = block.sample_count
                    offset = parts_by_seq[block.seq].column_offset
                    if offset is not None:
                        row_codes[offset : offset + count] = samples[
                            :, block.channels.index(channel)
                        ]
                        row_flags[offset : offset + count] = 0
                    while span is not None and span[0].seq == block.seq:
                        _, start, stop, reason = span
                        if offset is not None:
                            row_flags[offset + start : offset + stop] |= reason
                        span = next(spans, None)

                verified = scan_blocks(raw_path, consume)
                if verified != raw or span is not None:
                    raise ValueError("capture changed while reconstructing")
                if current_line is not None:
                    add_row(current_line, row_codes, row_flags)
                    next_line = current_line + 1
                add_missing(capture.lines)
            if sha256_file(raw_path) != capture.raw_sha256:
                raise ValueError("capture changed while reconstructing")
            valid_fraction = accumulator.write(scratch_dir, output)
    manifest = {
        "mode": "reconstruct",
        "algorithm": "bilinear-linear-domain-v1",
        "storage": "row-streamed disk scratch",
        "domain": "density",
        "unit": "OD",
        "calibration_id": calibration.calibration_id,
        "calibration_provenance": calibration.provenance.value,
        "coordinate_source": (
            "declared uniform capture coordinates"
            if model is None
            else "observable facet timing and calibrated target coordinates"
        ),
        "delay_offset_samples": offset_samples,
        "facet_gain_applied": bool(gains),
        "support": "positive bilinear weight within one grid pitch per axis",
        "minimum_support_weight": 0.5,
        "valid_fraction": valid_fraction,
        "grid": asdict(grid),
        "raw_sha256": capture.raw_sha256,
        "channel": channel,
        "capture_binding": capture.binding.model_dump(mode="json"),
        "calibration_binding_status": "compatible_at_reconstruction",
    }
    if raw is not None:
        if segmented:
            manifest["unplaced_blocks"] = sum(
                p.column_offset is None for parts in layouts.values() for p in parts
            )
            manifest["segmented_rows"] = sum(len(parts) > 1 for parts in layouts.values())
        mappings = {"uniform": nominal_x, **{f"phase_{k}": v for k, v in x_by_phase.items()}}
        lineage = {
            "format": "zlscan-image-lineage/2" if segmented else "zlscan-image-lineage/1",
            "raw_sha256": capture.raw_sha256,
            "calibration_id": calibration.calibration_id,
            "channel": channel,
            "session_id": raw.session.session_id,
            "origin": raw.session.origin,
            "clock_domains": raw.session.clock_domains,
            "grid": asdict(grid),
            "mappings": {
                key: [float(v) if np.isfinite(v) else None for v in values]
                for key, values in mappings.items()
            },
            "lines": lineage_rows,
        }
        (output / "lineage.json").write_text(
            json.dumps(lineage, allow_nan=False) + "\n", encoding="utf-8"
        )
        manifest["lineage_sha256"] = sha256_file(output / "lineage.json")
    manifest["density_sha256"] = sha256_file(output / "density.npz")
    if facet_path is not None:
        manifest["facet_timing_sha256"] = sha256_file(facet_path)
    if quality_path is not None:
        manifest["quality_sha256"] = sha256_file(quality_path)
    (output / "run.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
