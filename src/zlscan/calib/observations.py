"""Calibration inputs from delivered codes and an operator's reference target protocol."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from zlscan.contracts.calibration import Binding, CalibItem, CalibrationHeader, Provenance
from zlscan.io.line_image import reconstruct
from zlscan.io.manifest import sha256_file
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


def calibration_from_scan(raw_path: Path, protocol_path: Path) -> CalibrationHeader:
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

    items = {}
    for target in protocol.targets:
        if target.name not in {"dark", "white"}:
            continue
        rows = slice(target.first_line, target.first_line + target.lines)
        references = {
            str(channel): {
                "median": np.median(codes[rows], axis=0).tolist(),
                "std": np.std(codes[rows], axis=0, ddof=1).tolist(),
            }
            for channel, codes in image.codes.items()
        }
        items[f"{target.name}_raw_code"] = CalibItem(
            value=references, method="per-column median and sample standard deviation in ADC codes"
        )
    protocol_sha256 = sha256_file(protocol_path)
    provenance = Provenance(protocol.origin)
    calibration = CalibrationHeader(
        format_version=1,
        provenance=provenance,
        calibration_id=hashlib.sha256(f"code-reference-v1:{protocol_sha256}".encode()).hexdigest(),
        created_at=datetime.now(UTC).isoformat(),
        binds_to=binding,
        valid_conditions={"scan_direction": "forward"},
        generator={
            "name": "code_reference",
            "version": 1,
            "raw_sha256": protocol.raw_sha256,
            "protocol_sha256": protocol_sha256,
        },
        items=items,
    )
    return calibration
