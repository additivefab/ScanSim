"""Explicit scope CSV conversion; declarations do not certify measurements."""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Declaration(BaseModel):
    model_config = ConfigDict(
        extra="forbid", allow_inf_nan=False, strict=True, str_strip_whitespace=True
    )


class ScopeChannel(Declaration):
    column: str = Field(min_length=1)
    unit: Literal["V", "mV", "uV", "A", "mA", "uA"]
    measurement_point: str | None = Field(default=None, min_length=1)
    probe_ratio: float | None = Field(default=None, gt=0)
    coupling: Literal["AC", "DC", "GND"] | None = None
    bandwidth_limit_hz: float | None = Field(default=None, ge=0)
    probe_compensated: bool | None = None
    acquisition_id: str | None = Field(default=None, min_length=1)


class ScopeMetadata(Declaration):
    time_column: str = Field(min_length=1)
    time_unit: Literal["s", "ms", "us", "ns"]
    channels: list[ScopeChannel] = Field(min_length=1)
    source_kind: Literal["unknown", "declared_measurement", "synthetic"] = "unknown"
    device: str | None = Field(default=None, min_length=1)
    sample_interval_s: float | None = Field(default=None, gt=0)
    time_tolerance_s: float | None = Field(default=None, ge=0)
    trigger_source: str | None = Field(default=None, min_length=1)
    trigger_threshold_v: float | None = None
    time_origin: str | None = Field(default=None, min_length=1)
    session: str | None = Field(default=None, min_length=1)
    epoch: int | None = Field(default=None, ge=0)
    simultaneous: bool | None = None

    @model_validator(mode="after")
    def unique_columns(self) -> ScopeMetadata:
        names = [self.time_column, *(c.column for c in self.channels)]
        if len(set(names)) != len(names):
            raise ValueError("time and channel columns must be distinct")
        return self


_TIME_SCALE = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9}
_VALUE_SCALE = {"V": 1.0, "mV": 1e-3, "uV": 1e-6, "A": 1.0, "mA": 1e-3, "uA": 1e-6}


def import_scope_csv(path: Path, metadata: ScopeMetadata) -> dict:
    """Return SI observations and explicit import gates without fitting or alignment.

    The 64 MiB/1M-row limits bound this viewing importer; larger streams require a
    separate block adapter. Probe compensation is descriptive, never applied twice.
    """
    with Path(path).open("rb") as stream:
        payload = stream.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024:
        raise ValueError("scope CSV exceeds 64 MiB import limit")
    reader = csv.reader(io.StringIO(payload.decode("utf-8-sig")), strict=True)
    try:
        header = next(reader)
        if len(set(header)) != len(header):
            raise ValueError("duplicate CSV columns")
        selected = [metadata.time_column, *(c.column for c in metadata.channels)]
        indices = [header.index(name) for name in selected]
        rows = []
        for row in reader:
            if len(row) != len(header):
                raise ValueError("ragged CSV row")
            rows.append([float(row[i]) for i in indices])
            if len(rows) > 1_000_000:
                raise ValueError("scope CSV exceeds 1M row import limit")
    except (StopIteration, csv.Error) as exc:
        raise ValueError("empty or malformed CSV") from exc
    if len(rows) < 2:
        raise ValueError("at least two samples are required")
    data = np.asarray(rows, dtype=np.float64)
    with np.errstate(over="ignore", invalid="ignore"):
        time_s = data[:, 0] * _TIME_SCALE[metadata.time_unit]
        values = [data[:, i + 1] * _VALUE_SCALE[c.unit] for i, c in enumerate(metadata.channels)]
        intervals = np.diff(time_s)
    if (
        not np.isfinite(data).all()
        or not np.isfinite(time_s).all()
        or any(not np.isfinite(v).all() for v in values)
        or not np.isfinite(intervals).all()
        or not (intervals > 0).all()
    ):
        raise ValueError("samples must be finite and time strictly increasing")
    declaration = metadata.model_dump(mode="json")
    missing = [k for k, v in declaration.items() if v is None]
    for i, channel in enumerate(declaration["channels"]):
        missing.extend(f"channels.{i}.{k}" for k, v in channel.items() if v is None)
    issues = []
    if metadata.sample_interval_s is not None and metadata.time_tolerance_s is not None:
        if not np.all(np.abs(intervals - metadata.sample_interval_s) <= metadata.time_tolerance_s):
            issues.append("sample_interval_mismatch")
    if any(c.probe_compensated is False for c in metadata.channels):
        issues.append("probe_compensation_not_applied; values remain exported quantities")
    ids = {c.acquisition_id for c in metadata.channels}
    synchronization = "unestablished"
    if metadata.simultaneous is False or len(ids - {None}) > 1:
        synchronization = "non_simultaneous"
    elif not missing and not issues and metadata.simultaneous is True and len(ids) == 1:
        synchronization = "declared_same_acquisition"
    return {
        "format": "zlscan.scope-import/1",
        "source_name": Path(path).name,
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "source_kind": metadata.source_kind,
        "metadata": declaration,
        "time_s": time_s.tolist(),
        "channels": [
            dict(column=c.column, unit_si=c.unit[-1], values_si=v.tolist())
            for c, v in zip(metadata.channels, values, strict=True)
        ],
        "missing_metadata": missing,
        "issues": issues,
        "synchronization": synchronization,
        "hard_acceptance_eligible": False,
        "parameter_upgrades": [],
        "interpolated": False,
        "alignment_uncertainty_s": None,
    }
