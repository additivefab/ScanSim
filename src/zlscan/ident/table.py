"""Scalar observation tables with explicit units, uncertainty semantics and lineage."""

from __future__ import annotations

import csv
import hashlib
import io
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from zlscan.ident.scope import Declaration
from zlscan.io.manifest import sha256_file

COLUMNS = (
    "measurement_id",
    "reference_id",
    "partition",
    "quantity",
    "value",
    "unit",
    "uncertainty",
    "uncertainty_kind",
    "coverage_factor",
    "uncertainty_source",
    "source_sha256",
    "session",
    "epoch",
    "acquisition_id",
)
_UNITS = {
    "V": ("voltage", 1.0, "V"),
    "mV": ("voltage", 1e-3, "V"),
    "uV": ("voltage", 1e-6, "V"),
    "A": ("current", 1.0, "A"),
    "mA": ("current", 1e-3, "A"),
    "uA": ("current", 1e-6, "A"),
    "s": ("time", 1.0, "s"),
    "ms": ("time", 1e-3, "s"),
    "us": ("time", 1e-6, "s"),
    "ns": ("time", 1e-9, "s"),
    "Hz": ("frequency", 1.0, "Hz"),
    "kHz": ("frequency", 1e3, "Hz"),
    "MHz": ("frequency", 1e6, "Hz"),
    "OD": ("density", 1.0, "OD"),
    "1": ("transmittance", 1.0, "1"),
    "code": ("adc_code", 1.0, "code"),
}


class SourceFile(Declaration):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TableMetadata(Declaration):
    source_kind: Literal["unknown", "declared_measurement", "synthetic"] = "unknown"
    device: str | None = Field(default=None, min_length=1)
    measurement_point: str | None = Field(default=None, min_length=1)
    probe_ratio: float | None = Field(default=None, gt=0)
    coupling: Literal["AC", "DC", "GND"] | None = None
    bandwidth_limit_hz: float | None = Field(default=None, ge=0)
    sample_interval_s: float | None = Field(default=None, gt=0)
    trigger_source: str | None = Field(default=None, min_length=1)
    trigger_threshold_v: float | None = None
    time_origin: str | None = Field(default=None, min_length=1)
    simultaneous: bool | None = None
    source_files: list[SourceFile] = Field(default_factory=list)


class MeasurementRow(Declaration):
    measurement_id: str = Field(min_length=1)
    reference_id: str | None = Field(default=None, min_length=1)
    partition: Literal["fit", "validation", "unassigned"] | None = None
    quantity: Literal[
        "voltage", "current", "time", "frequency", "density", "transmittance", "adc_code"
    ]
    value: float | None = None
    unit: str
    uncertainty: float | None = Field(default=None, ge=0)
    uncertainty_kind: Literal["standard", "expanded", "absolute_bound"] | None = None
    coverage_factor: float | None = Field(default=None, gt=0)
    uncertainty_source: str | None = Field(default=None, min_length=1)
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    session: str | None = Field(default=None, min_length=1)
    epoch: int | None = Field(default=None, ge=0, lt=2**32)
    acquisition_id: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def compatible_unit(self) -> MeasurementRow:
        if self.unit not in _UNITS or _UNITS[self.unit][0] != self.quantity:
            raise ValueError("unit does not match the declared quantity")
        return self


def _parse_rows(payload: bytes) -> list[MeasurementRow]:
    reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
    try:
        header = next(reader)
        if len(header) != len(COLUMNS) or set(header) != set(COLUMNS):
            raise ValueError("table header must contain exactly the v1 measurement columns")
        rows = []
        for values in reader:
            if len(values) != len(header):
                raise ValueError("ragged measurement table row")
            data = {k: v.strip() or None for k, v in zip(header, values, strict=True)}
            for k in ["value", "uncertainty", "coverage_factor"]:
                if data[k] is not None:
                    data[k] = float(data[k])
            if data["epoch"] is not None:
                data["epoch"] = int(data["epoch"])
            rows.append(MeasurementRow.model_validate(data))
            if len(rows) > 100_000:
                raise ValueError("measurement table exceeds 100000 row limit")
    except (csv.Error, StopIteration) as exc:
        raise ValueError("empty or malformed measurement table") from exc
    if not rows:
        raise ValueError("measurement table needs at least one row")
    ids = [r.measurement_id for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate measurement_id")
    return rows


def import_measurement_table(path: Path, metadata: TableMetadata, base: Path) -> dict:
    """Read scalar declarations; check hashes and reuse without fitting or upgrading evidence.

    ``base`` is the metadata file's parent. Listed source files are explicit inputs;
    a verified digest establishes byte identity, never measurement authenticity.
    """
    with Path(path).open("rb") as stream:
        payload = stream.read(32 * 1024 * 1024 + 1)
    if len(payload) > 32 * 1024 * 1024:
        raise ValueError("measurement table exceeds 32 MiB limit")
    rows = _parse_rows(payload)
    verified = set()
    for source in metadata.source_files:
        if sha256_file(Path(base) / source.path) != source.sha256:
            raise ValueError(f"source SHA256 mismatch: {source.path}")
        verified.add(source.sha256)
    result_rows = []
    groups: dict[tuple, dict[str, list[str]]] = {}
    lineage_complete = True
    for row in rows:
        declared = row.model_dump(mode="json")
        missing = [k for k, v in declared.items() if v is None and k != "coverage_factor"]
        if row.uncertainty_kind == "expanded" and row.coverage_factor is None:
            missing.append("coverage_factor")
        issues = []
        if row.coverage_factor is not None and row.uncertainty_kind != "expanded":
            issues.append("coverage_factor_without_expanded_uncertainty")
        scale, unit = _UNITS[row.unit][1:]
        value = row.value * scale if row.value is not None else None
        uncertainty = row.uncertainty * scale if row.uncertainty is not None else None
        if any(v is not None and not math.isfinite(v) for v in [value, uncertainty]):
            raise ValueError("SI conversion is nonfinite")
        if row.quantity == "transmittance" and value is not None and not 0 <= value <= 1:
            issues.append("transmittance_outside_0_1; value_preserved")
        source_verified = row.source_sha256 in verified
        result_rows.append(
            dict(
                declared=declared,
                value_si=value,
                uncertainty_si=uncertainty,
                unit_si=unit,
                source_verified=source_verified,
                missing_metadata=missing,
                issues=issues,
            )
        )
        capture = (row.session, row.epoch, row.acquisition_id)
        if not source_verified or None in capture or row.partition not in {"fit", "validation"}:
            lineage_complete = False
        if row.partition in {"fit", "validation"}:
            keys = []
            if row.source_sha256 is not None:
                keys.append(("source_sha256", row.source_sha256))
            if None not in capture:
                keys.append(("capture_identity", *capture))
            for key in keys:
                partitions = groups.setdefault(key, {"fit": [], "validation": []})
                partitions[row.partition].append(row.measurement_id)
    leakage = [
        dict(kind=k[0], identity=list(k[1:]), **v)
        for k, v in groups.items()
        if v["fit"] and v["validation"]
    ]
    roles = {r.partition for r in rows}
    if leakage:
        status = "leakage_detected"
    elif lineage_complete and {"fit", "validation"} <= roles:
        status = "no_declared_reuse_detected"
    else:
        status = "not_assessed"
    declaration = metadata.model_dump(mode="json")
    return {
        "format": "zlscan.measurement-table/1",
        "source_name": Path(path).name,
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "source_kind": metadata.source_kind,
        "metadata": declaration,
        "missing_metadata": [k for k, v in declaration.items() if v is None],
        "rows": result_rows,
        "verified_source_sha256": sorted(verified),
        "partition_status": status,
        "leakage": leakage,
        "independence_established": False,
        "synchronization": "not_applicable_scalar_table",
        "hard_acceptance_eligible": False,
        "parameter_upgrades": [],
    }
