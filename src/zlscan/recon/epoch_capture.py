"""Observed-epoch calibration selection and row-streamed linear-domain deposition."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.contracts.calibration import (
    CalibrationHeader,
    CalibrationSet,
    CaptureDescription,
    resolve_calibrations,
)
from zlscan.contracts.flags import InvalidFlag
from zlscan.io.facet_timing import FacetTiming, GeometryModel, observed_phases
from zlscan.io.line_layout import line_layout, source_record
from zlscan.io.manifest import sha256_file
from zlscan.io.quality import quality_spans
from zlscan.io.rawstream import FLAG_LINE_START, scan_blocks
from zlscan.recon.density import Grid, _prepare_transfer, _sample_transmittance
from zlscan.recon.disk_density import DiskDensity


def reconstruct_epoch_capture(
    raw_path: Path,
    capture: CaptureDescription,
    calibration: CalibrationHeader | CalibrationSet,
    grid: Grid,
    output: Path,
    *,
    channel: int,
    quality_path: Path | None,
    facet_path: Path | None,
) -> dict:
    from zlscan.recon.capture import _validate_capture

    capture = CaptureDescription.model_validate(capture.model_dump(mode="json"))
    calibration = type(calibration).model_validate(calibration.model_dump(mode="json"))
    if sha256_file(raw_path) != capture.raw_sha256:
        raise ValueError("capture SHA-256 does not match its description")
    raw = scan_blocks(raw_path, lambda *_: None)
    session = _validate_capture(raw, capture)
    for block in raw.blocks:
        binding = capture.binding_for_epoch(block.epoch)
        if (session.origin == "SIMULATED") != (binding.simulation_hash is not None):
            raise ValueError("epoch binding and capture origin do not match")
    blocks = [b for b in raw.blocks if channel in b.channels]
    if not blocks:
        raise ValueError(f"capture lacks channel {channel}")
    cols = int(session.windows["window_length"])
    if cols <= 0 or any(b.sample_count > cols for b in blocks):
        raise ValueError("capture block is larger than its declared sample window")
    selected = resolve_calibrations(calibration, capture, [b.epoch for b in blocks])
    layouts = line_layout(raw.blocks, channel, cols)
    parts_by_seq = {p.block.seq: p for parts in layouts.values() for p in parts}
    anchor_epochs = {b.line_number: b.epoch for b in blocks if b.flags & FLAG_LINE_START}
    columns = np.arange(cols) + 0.5
    nominal_x = grid.origin_x_m + columns / cols * grid.width * grid.pitch_x_m
    mappings = {"unobserved_nominal": nominal_x}
    timing = FacetTiming.model_validate_json(facet_path.read_bytes()) if facet_path else None
    phases = None
    projections = {}
    for epoch, cal in selected.items():
        transfer = _prepare_transfer(cal, str(channel), cols)
        geometry = cal.items.get("facet_geometry")
        model = (
            GeometryModel.model_validate(geometry.value)
            if geometry and geometry.status == "ok"
            else None
        )
        delay = cal.items.get("delay")
        offset = 0.0
        delay_used = False
        if delay and delay.status == "ok":
            if not isinstance(delay.value, dict) or type(delay.value.get("delay_ticks")) is not int:
                raise ValueError("calibration delay requires an integer delay_ticks")
            fitted = delay.value.get("channel")
            if fitted is None and len(session.channel_roles) > 1:
                raise ValueError("multi-channel capture requires channel-specific electronic delay")
            if fitted is not None and type(fitted) is not int:
                raise ValueError("invalid electronic delay channel")
            if (fitted is None or fitted == channel) and model is None:
                domains = {b.clock_domain for b in blocks if b.epoch == epoch}
                clocks = [c for c in session.clock_domains if c["id"] in domains]
                if len(clocks) != 1:
                    raise ValueError("epoch delay compensation requires one observed sample clock")
                clock = clocks[0]
                offset = delay.value["delay_ticks"] * clock["freq_num"] / (1e12 * clock["freq_den"])
                delay_used = True
        uniform = f"epoch_{epoch}:uniform"
        mappings[uniform] = (
            grid.origin_x_m + (columns - offset) / cols * grid.width * grid.pitch_x_m
        )
        gains = None
        if model is not None:
            if timing is None or timing.policy != model.policy or session.windows != model.window:
                raise ValueError(
                    "epoch facet geometry requires matching observed policy and window"
                )
            if phases is None:
                phases = observed_phases(timing, raw, capture.raw_sha256, capture.lines)
            u = 2 * columns / cols - 1
            for phase, fit in model.facets.items():
                a, b, c = fit.x_coefficients_m
                lo, hi = fit.support_u
                mappings[f"epoch_{epoch}:phase_{phase}"] = np.where(
                    (u >= lo) & (u <= hi), a + b * u + c * u**3, np.nan
                )
            gain = cal.items.get("facet_gain")
            gains = (
                gain.value["channels"].get(str(channel), {})
                if gain and gain.status == "ok"
                else None
            )
        projections[epoch] = dict(
            transfer=transfer,
            model=model,
            gains=gains,
            uniform=uniform,
            delay_used=delay_used,
            offset_samples=offset,
        )
    if timing is not None and not any(p["model"] is not None for p in projections.values()):
        raise ValueError("facet timing has no corresponding epoch geometry")

    spans = (
        iter(())
        if quality_path is None
        else quality_spans(quality_path, capture.raw_sha256, raw, channel, capture.lines, cols)
    )
    span = next(spans, None)
    missing = int(InvalidFlag.MISSING_SAMPLE)
    lineage_rows = []
    with tempfile.TemporaryDirectory(prefix="zlscan_epoch_density_") as directory:
        scratch_dir = Path(directory)
        with (scratch_dir / "rows.bin").open("w+b") as scratch:
            accumulator = DiskDensity(scratch, grid, next(iter(selected.values())), channel, cols)

            def add_row(line, codes, flags, epochs):
                y_nominal = (
                    grid.origin_y_m + (line + 0.5) / capture.lines * grid.height * grid.pitch_y_m
                )
                x = nominal_x.copy()
                y = np.full(cols, y_nominal)
                transmittance = np.full(cols, np.nan)
                plans = {}
                for epoch in sorted(
                    set(epochs[epochs >= 0].tolist())
                    | {part.block.epoch for part in layouts.get(line, [])}
                ):
                    p = projections[epoch]
                    selected_columns = epochs == epoch
                    reasons = flags.copy()
                    values = _sample_transmittance(codes, reasons, p["transfer"])
                    mapping, gain, yy = p["uniform"], 1.0, y_nominal
                    model = p["model"]
                    if model is not None:
                        phase = str(phases[line]) if anchor_epochs.get(line) == epoch else "-1"
                        fit = model.facets.get(phase)
                        if fit is None:
                            reasons |= int(InvalidFlag.FACET_UNRESOLVED)
                        else:
                            mapping = f"epoch_{epoch}:phase_{phase}"
                            yy += fit.y_offset_m or 0.0
                            if p["gains"] is not None:
                                if phase in p["gains"]:
                                    gain = p["gains"][phase]
                                else:
                                    reasons |= int(InvalidFlag.WHITE_REFERENCE_INVALID)
                    if not np.isfinite(gain) or gain <= 0:
                        raise ValueError("facet gain must be positive and finite")
                    transmittance[selected_columns] = values[selected_columns] / gain
                    flags[selected_columns] = reasons[selected_columns]
                    x[selected_columns] = mappings[mapping][selected_columns]
                    y[selected_columns] = yy
                    plans[epoch] = dict(
                        mapping=mapping,
                        y_m=yy,
                        coordinate_source=(
                            "observable_facet_calibration"
                            if mapping != p["uniform"]
                            else "declared_uniform_grid"
                        ),
                        delay_calibration_used=p["delay_used"],
                        facet_mapping_status=(
                            "not_available"
                            if model is None
                            else "resolved"
                            if mapping != p["uniform"]
                            else "unresolved"
                        ),
                    )
                segments = []
                for part in layouts.get(line, []):
                    epoch = part.block.epoch
                    # An unplaced segment retains its identity and calibration but gets no pixels.
                    placement = plans.get(
                        epoch, dict(mapping=projections[epoch]["uniform"], y_m=y_nominal)
                    )
                    segments.append(
                        dict(
                            source=source_record(part.block),
                            column_offset=part.column_offset,
                            placement_reason=part.reason,
                            calibration_id=selected[epoch].calibration_id,
                            **placement,
                        )
                    )
                lineage_rows.append(
                    dict(line=line, mapping="unobserved_nominal", y_m=y_nominal, segments=segments)
                )
                accumulator.add_linear(transmittance, flags, x, y)

            current_line = None
            next_line = 0
            row_codes = row_flags = row_epochs = None

            def add_missing(until):
                nonlocal next_line
                while next_line < until:
                    add_row(
                        next_line,
                        np.zeros(cols, np.uint16),
                        np.full(cols, missing, np.uint16),
                        np.full(cols, -1, np.int64),
                    )
                    next_line += 1

            def consume(_session, block, samples):
                nonlocal span, current_line, next_line, row_codes, row_flags, row_epochs
                if channel not in block.channels:
                    return
                if current_line is not None and block.line_number != current_line:
                    add_row(current_line, row_codes, row_flags, row_epochs)
                    next_line = current_line + 1
                if not next_line <= block.line_number < capture.lines or block.sample_count > cols:
                    raise ValueError("capture line layout changed while reconstructing")
                if current_line != block.line_number:
                    add_missing(block.line_number)
                    current_line = block.line_number
                    row_codes = np.zeros(cols, np.uint16)
                    row_flags = np.full(cols, missing, np.uint16)
                    row_epochs = np.full(cols, -1, np.int64)
                offset = parts_by_seq[block.seq].column_offset
                if offset is not None:
                    sl = slice(offset, offset + block.sample_count)
                    row_codes[sl] = samples[:, block.channels.index(channel)]
                    row_flags[sl] = 0
                    row_epochs[sl] = block.epoch
                while span is not None and span[0].seq == block.seq:
                    _, start, stop, reason = span
                    if offset is not None:
                        row_flags[offset + start : offset + stop] |= reason
                    span = next(spans, None)

            verified = scan_blocks(raw_path, consume)
            if verified != raw or span is not None or sha256_file(raw_path) != capture.raw_sha256:
                raise ValueError("capture changed while reconstructing")
            if current_line is not None:
                add_row(current_line, row_codes, row_flags, row_epochs)
                next_line = current_line + 1
            add_missing(capture.lines)
            valid_fraction = accumulator.write(scratch_dir, output)
    document = calibration.model_dump(mode="json")
    encoded = calibration.model_dump_json().encode("utf-8")
    identity = (
        calibration.calibration_set_id
        if isinstance(calibration, CalibrationSet)
        else calibration.calibration_id
    )
    lineage = dict(
        format="zlscan-image-lineage/3",
        raw_sha256=capture.raw_sha256,
        calibration_id=identity,
        channel=channel,
        session_id=session.session_id,
        origin=session.origin,
        clock_domains=session.clock_domains,
        grid=asdict(grid),
        epoch_calibrations={str(k): v.calibration_id for k, v in selected.items()},
        mappings={
            k: [float(x) if np.isfinite(x) else None for x in v] for k, v in mappings.items()
        },
        lines=lineage_rows,
    )
    (output / "lineage.json").write_text(
        json.dumps(lineage, allow_nan=False) + "\n", encoding="utf-8"
    )
    manifest = dict(
        mode="reconstruct",
        algorithm="epoch-bilinear-linear-domain-v1",
        storage="row-streamed disk scratch",
        domain="density",
        unit="OD",
        calibration_id=identity,
        calibration_provenance=next(iter(selected.values())).provenance.value,
        calibration_document=document,
        calibration_document_sha256=hashlib.sha256(encoded).hexdigest(),
        calibration_document_hash_format="pydantic canonical JSON UTF-8",
        epoch_calibrations=lineage["epoch_calibrations"],
        coordinate_source="per-segment declared or observable/calibrated sources; see lineage",
        epoch_coordinate_sources={
            str(epoch): dict(
                spatial_model_available=p["model"] is not None,
                delay_calibration_used=p["delay_used"],
                applied_separate_delay_offset_samples=p["offset_samples"],
            )
            for epoch, p in projections.items()
        },
        support="positive bilinear weight within one grid pitch per axis",
        minimum_support_weight=0.5,
        valid_fraction=valid_fraction,
        grid=asdict(grid),
        raw_sha256=capture.raw_sha256,
        channel=channel,
        capture_binding=capture.binding.model_dump(mode="json"),
        capture_description=capture.model_dump(mode="json"),
        calibration_binding_status="compatible_per_epoch_at_reconstruction",
        unplaced_blocks=sum(p.column_offset is None for parts in layouts.values() for p in parts),
        segmented_rows=sum(len(parts) > 1 for parts in layouts.values()),
        lineage_sha256=sha256_file(output / "lineage.json"),
        density_sha256=sha256_file(output / "density.npz"),
    )
    for name, path in (("quality", quality_path), ("facet_timing", facet_path)):
        if path is not None:
            manifest[name + "_sha256"] = sha256_file(path)
    (output / "run.json").write_text(
        json.dumps(manifest, allow_nan=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
