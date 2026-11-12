"""Calibration inputs from delivered codes and an operator's reference target protocol."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, StrictInt, model_validator

from zlscan.calib.delay import fit_optical_registration, fit_synchronous_response
from zlscan.calib.facet_targets import fit_facet_gain, fit_observed_targets
from zlscan.calib.radiometric import fit_log_transfer
from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader, Provenance
from zlscan.contracts.events import Event
from zlscan.contracts.time import ClockDomain
from zlscan.io.line_image import reconstruct
from zlscan.io.manifest import sha256_file
from zlscan.io.quality import apply_quality_log
from zlscan.io.rawstream import read_all


class TargetScan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["dark", "white", "wedge", "edge", "grid", "slanted_line"]
    first_line: int = Field(ge=0)
    lines: int = Field(ge=2)


class ScanProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    origin: Literal["SIMULATED", "MEASURED"]
    binding: Binding
    raw_sha256: str
    width_m: float = Field(gt=0)
    pitch_y_m: float = Field(gt=0)
    targets: list[TargetScan]
    wedge_density: list[float]
    edge_fraction: float
    grid_line_x_fraction: list[float]
    slanted_line_x_fractions: tuple[float, float]

    @model_validator(mode="after")
    def _contiguous_targets(self) -> ScanProtocol:
        if (self.origin == "SIMULATED") != (self.binding.simulation_hash is not None):
            raise ValueError("simulation_hash is required only for simulated reference scans")
        next_line = 0
        seen = set()
        for target in self.targets:
            if target.first_line != next_line or target.name in seen:
                raise ValueError("target ranges must be contiguous and unique")
            next_line += target.lines
            seen.add(target.name)
        if not {"dark", "white"} <= seen:
            raise ValueError("calibration requires dark and white targets")
        return self


class InjectionTrace(BaseModel):
    """Observation from a synchronized test injection associated with this capture."""

    model_config = ConfigDict(extra="forbid")

    raw_sha256: str
    ticks: list[StrictInt]
    values: list[FiniteFloat]
    monitor: Event
    channel: StrictInt | None = Field(default=None, ge=0)


def calibration_from_scan(
    raw_path: Path,
    protocol_path: Path,
    *,
    fit_transfer: bool = False,
    timing_path: Path | None = None,
    quality_path: Path | None = None,
    facet_path: Path | None = None,
    edge_registration: bool = False,
) -> CalibrationHeader:
    """Create empirical code-domain references from a complete calibration scan."""
    protocol = ScanProtocol.model_validate_json(protocol_path.read_bytes())
    if sha256_file(raw_path) != protocol.raw_sha256:
        raise ValueError("calibration capture SHA-256 does not match the target protocol")
    raw = read_all(raw_path)
    session = raw.session
    if session is None or raw.fatal or session.origin != protocol.origin:
        raise ValueError("calibration capture origin does not match the target protocol")
    binding = protocol.binding
    if session.channel_roles != binding.channel_roles:
        raise ValueError("calibration capture channel roles do not match the target protocol")
    if protocol.origin == "MEASURED":
        for field in ("hardware_hash", "wiring_rev", "firmware_hash", "register_dump_hash"):
            value = getattr(session, field)
            if value is None or value != getattr(binding, field):
                raise ValueError(f"calibration capture {field} does not match the target protocol")
    epochs = {block.epoch for block in raw.blocks}
    if epochs != {binding.epoch_range[0]} or binding.epoch_range[0] != binding.epoch_range[1]:
        raise ValueError("reference scans must belong to one declared calibration epoch")
    expected_lines = range(sum(target.lines for target in protocol.targets))
    if [block.line_number for block in raw.blocks] != list(expected_lines):
        raise ValueError("reference scan requires one ordered block for each declared line")
    image = reconstruct(raw, expected_lines)
    if image.status != "complete":
        raise ValueError("calibration capture has missing or invalid samples")

    channel_flags = dict.fromkeys(image.codes, image.invalid)
    if quality_path is not None:
        for channel in image.codes:
            flags = image.invalid.copy()
            apply_quality_log(quality_path, protocol.raw_sha256, raw, channel, flags)
            channel_flags[channel] = flags
            for target in protocol.targets:
                used = target.name in {"dark", "white"} or (fit_transfer and target.name == "wedge")
                if used and flags[target.first_line : target.first_line + target.lines].any():
                    raise ValueError(f"quality events invalidate the {target.name} reference")

    items = {}
    by_name = {target.name: target for target in protocol.targets}
    for target in protocol.targets:
        if target.name not in {"dark", "white"}:
            continue
        rows = slice(target.first_line, target.first_line + target.lines)
        references = {
            str(channel): {
                "median": np.median(codes[rows], axis=0).tolist(),
                "std": np.std(codes[rows], axis=0, ddof=1).tolist(),
                "median_drift_code": float(
                    np.median(codes[rows][-1].astype(float) - codes[rows][0])
                ),
            }
            for channel, codes in image.codes.items()
        }
        items[f"{target.name}_raw_code"] = CalibItem(
            value=references, method="per-column median and sample standard deviation in ADC codes"
        )
    if fit_transfer:
        if "wedge" not in by_name:
            raise ValueError("a density wedge is required to fit a transfer")
        slices = [
            slice(by_name[name].first_line, by_name[name].first_line + by_name[name].lines)
            for name in ("dark", "white", "wedge")
        ]
        transfers = {}
        for channel, codes in image.codes.items():
            dark, white, wedge = (codes[selection] for selection in slices)
            transfers[str(channel)] = asdict(
                fit_log_transfer(dark, white, wedge, np.asarray(protocol.wedge_density))
            )
        items["transfer_lut"] = CalibItem(
            value={"channels": transfers, "extrapolation": "invalid"},
            method="logarithmic fit to dark, white and interior wedge patch medians",
        )
    algorithm = "log_transfer" if fit_transfer else "code_reference"
    protocol_sha256 = sha256_file(protocol_path)
    sources = {"raw_sha256": protocol.raw_sha256, "protocol_sha256": protocol_sha256}
    if timing_path is not None:
        timing = InjectionTrace.model_validate_json(timing_path.read_bytes())
        if (
            timing.raw_sha256 != protocol.raw_sha256
            or timing.monitor.origin.value != session.origin
            or timing.monitor.clock_domain not in {clock["id"] for clock in session.clock_domains}
        ):
            raise ValueError("injection trace does not belong to this calibration capture")
        channel = timing.channel
        if channel is None:
            if len(image.codes) != 1:
                raise ValueError("multi-channel injection trace requires an explicit channel")
            channel = next(iter(image.codes))
        if channel not in image.codes:
            raise ValueError("injection trace names an absent ADC channel")
        fit = fit_synchronous_response(
            np.asarray(timing.ticks, dtype=np.int64),
            np.asarray(timing.values),
            timing.monitor,
            session_id=session.session_id,
            epoch=binding.epoch_range[0],
        )
        items["delay"] = CalibItem(
            value={**asdict(fit), "channel": channel},
            method="observable synchronous injection and first-order step fit",
        )
        sources["timing_sha256"] = sha256_file(timing_path)
    if edge_registration:
        edge = by_name.get("edge")
        if edge is None:
            raise ValueError("optical registration requires an edge target scan")
        selection = slice(edge.first_line, edge.first_line + edge.lines)
        declared = {clock["id"]: ClockDomain(**clock) for clock in session.clock_domains}
        registrations = {}
        for channel, codes in image.codes.items():
            clock_ids = {
                block.clock_domain for block in raw.blocks[selection] if channel in block.channels
            }
            if len(clock_ids) != 1 or not clock_ids <= declared.keys():
                registrations[str(channel)] = {
                    "status": "unidentifiable",
                    "reason": "ambiguous sample clock",
                }
                continue
            try:
                fit = fit_optical_registration(
                    codes[selection],
                    channel_flags[channel][selection],
                    declared[next(iter(clock_ids))],
                    protocol.edge_fraction,
                )
            except ValueError as exc:
                registrations[str(channel)] = {"status": "unidentifiable", "reason": str(exc)}
            else:
                registrations[str(channel)] = {"status": "ok", **asdict(fit)}
        items["optical_registration"] = CalibItem(
            value={"channels": registrations},
            method="optical edge offset relative to nominal target position",
        )
    if quality_path is not None:
        sources["quality_sha256"] = sha256_file(quality_path)
    if facet_path is not None:
        grid = by_name.get("grid")
        if grid is None:
            raise ValueError("facet calibration requires a grid target scan")
        slanted = by_name.get("slanted_line")
        grid_lines = range(grid.first_line, grid.first_line + grid.lines)
        slanted_lines = (
            range(slanted.first_line, slanted.first_line + slanted.lines) if slanted else range(0)
        )
        model, phases = fit_observed_targets(
            facet_path,
            raw,
            protocol.raw_sha256,
            channel_flags,
            grid_lines,
            slanted_lines,
            image.codes,
            protocol.grid_line_x_fraction,
            protocol.width_m,
            range(by_name["dark"].first_line, by_name["dark"].first_line + by_name["dark"].lines),
            range(
                by_name["white"].first_line, by_name["white"].first_line + by_name["white"].lines
            ),
        )
        items["facet_geometry"] = CalibItem(
            value=model,
            method="observed grid landmarks grouped by observable facet phase",
        )
        sources["facet_sha256"] = sha256_file(facet_path)
        if fit_transfer:
            white = by_name["white"]
            white_rows = slice(white.first_line, white.first_line + white.lines)
            by_channel = {}
            for channel, codes in image.codes.items():
                transfer = items["transfer_lut"].value["channels"][str(channel)]
                dark_codes = items["dark_raw_code"].value[str(channel)]["median"]
                measured_white = codes[white_rows]
                locked_phases = phases[white_rows]
                gains = fit_facet_gain(
                    measured_white, locked_phases, np.asarray(dark_codes), transfer
                )
                if gains:
                    by_channel[str(channel)] = gains
                    # Keep the LUT bounded by observed references while covering the locked groups.
                    columns = np.ones(measured_white.shape[1], dtype=bool)
                    columns[list(transfer["white_outlier_columns"])] = False
                    observed_codes = [
                        np.median(measured_white[locked_phases == int(phase)][:, columns], axis=0)
                        for phase in gains
                    ]
                    observed_codes = np.concatenate(observed_codes)
                    prior = transfer["code_knots"]
                    knots = np.linspace(
                        min(prior[0], observed_codes.min()),
                        max(prior[-1], observed_codes.max()),
                        len(prior),
                    )
                    slope = transfer["slope_codes_per_decade"]
                    intercept = transfer["intercept_code"]
                    transfer["code_knots"] = knots.tolist()
                    transfer["intensity_knots"] = (10 ** ((knots - intercept) / slope)).tolist()
            items["facet_gain"] = CalibItem(
                status="ok" if by_channel else "unidentifiable",
                value={"channels": by_channel} if by_channel else None,
                method="relative white-field amplitude grouped by observable facet phase",
            )
    source_ids = ":".join(sources.values()) if len(sources) > 2 else protocol_sha256
    if edge_registration:
        source_ids += ":optical-registration-v1"
    provenance = Provenance(protocol.origin)
    calibration = CalibrationHeader(
        format_version=1,
        provenance=provenance,
        calibration_id=hashlib.sha256(f"{algorithm}-v1:{source_ids}".encode()).hexdigest(),
        created_at=datetime.now(UTC).isoformat(),
        binds_to=binding,
        valid_conditions={"scan_direction": "forward"},
        generator={
            "name": algorithm,
            "version": 1,
            **sources,
        },
        items=items,
    )
    return calibration
