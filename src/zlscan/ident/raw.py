"""Read-only ZLRAW snapshot import; hardware protocol compatibility remains external."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from zlscan.ident.scope import Declaration
from zlscan.io.rawstream import scan_blocks


class RawChannel(Declaration):
    channel: int = Field(ge=0, lt=16)
    measurement_point: str | None = Field(default=None, min_length=1)
    probe_ratio: float | None = Field(default=None, gt=0)
    coupling: Literal["AC", "DC", "GND"] | None = None
    bandwidth_limit_hz: float | None = Field(default=None, ge=0)
    acquisition_id: str | None = Field(default=None, min_length=1)


class RawMetadata(Declaration):
    source_kind: Literal["unknown", "declared_measurement", "synthetic"] = "unknown"
    device: str | None = Field(default=None, min_length=1)
    session_id: int | None = Field(default=None, ge=0, lt=2**64)
    epochs: list[Annotated[int, Field(ge=0, lt=2**32)]] | None = Field(default=None, min_length=1)
    channels: list[RawChannel] | None = Field(default=None, min_length=1)
    sample_interval_s: float | None = Field(default=None, gt=0)
    trigger_source: str | None = Field(default=None, min_length=1)
    trigger_threshold_v: float | None = None
    time_origin: str | None = Field(default=None, min_length=1)
    simultaneous: bool | None = None

    @model_validator(mode="after")
    def unique_identities(self) -> RawMetadata:
        if self.channels is not None:
            ids = [c.channel for c in self.channels]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate channel declarations")
        if self.epochs is not None and len(set(self.epochs)) != len(self.epochs):
            raise ValueError("duplicate epoch declarations")
        return self


def import_raw_capture(source: Path, metadata: RawMetadata, output: Path) -> dict:
    """Copy an immutable viewing artifact, then validate that snapshot block by block.

    Payload previews are bounded to 256 sample rows. The existing reader and this
    manifest retain block metadata, so this is not an unbounded capture recorder.
    Failure leaves a forensic copy; an output directory is never reused.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / "raw.zlraw"
    digest = hashlib.sha256()
    with Path(source).open("rb") as stream, snapshot.open("xb") as target:
        initial = os.fstat(stream.fileno())
        while chunk := stream.read(1024 * 1024):
            target.write(chunk)
            digest.update(chunk)
        final = os.fstat(stream.fileno())
        if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (
            final.st_size,
            final.st_mtime_ns,
            final.st_ctime_ns,
        ):
            raise ValueError("source changed during snapshot copy; output is not a valid import")
    preview = []
    remaining = 256
    sample_rows = 0
    payload_issues = []

    def consume(session, block, codes):
        nonlocal remaining, sample_rows
        sample_rows += block.sample_count
        if not block.channels or block.sample_count == 0:
            payload_issues.append(f"empty_block:{block.seq}")
        if not 1 <= session.bits_per_sample <= 16:
            payload_issues.append("unsupported_bits_per_sample")
        elif codes.size and int(codes.max()) >= 2**session.bits_per_sample:
            payload_issues.append(f"code_outside_declared_bits:{block.seq}")
        if block.first_sample_index + block.sample_count > 2**64:
            payload_issues.append(f"sample_index_overflow:{block.seq}")
        if remaining and not payload_issues:
            n = min(remaining, block.sample_count)
            preview.append(
                {
                    "epoch": block.epoch,
                    "clock_domain": block.clock_domain,
                    "first_sample_index": block.first_sample_index,
                    "channels": block.channels,
                    "codes": codes[:n].tolist(),
                }
            )
            remaining -= n

    read = scan_blocks(snapshot, consume)
    declaration = metadata.model_dump(mode="json")
    missing = [k for k, v in declaration.items() if v is None]
    for i, c in enumerate(declaration["channels"] or []):
        missing.extend(f"channels.{i}.{k}" for k, v in c.items() if v is None)
    issues = list(dict.fromkeys(payload_issues))
    session = read.session
    if session is not None:
        if metadata.session_id is not None and metadata.session_id != session.session_id:
            issues.append("session_identity_mismatch")
        epochs = {b.epoch for b in read.blocks}
        if metadata.epochs is not None and set(metadata.epochs) != epochs:
            issues.append("epoch_identity_mismatch")
        if (session.origin == "SIMULATED" and metadata.source_kind == "declared_measurement") or (
            session.origin == "MEASURED" and metadata.source_kind == "synthetic"
        ):
            issues.append("origin_declaration_conflict")
    channels = metadata.channels or []
    declared_ids = {c.channel for c in channels}
    if channels and any(set(b.channels) != declared_ids for b in read.blocks):
        issues.append("channel_identity_mismatch")
    domains = {b.clock_domain for b in read.blocks}
    if len(domains) > 1:
        issues.append("multiple_clock_domains; alignment_not_established")
    acquisition_ids = {c.acquisition_id for c in channels}
    if len(acquisition_ids - {None}) > 1:
        issues.append("different_acquisitions")
    if read.fatal or session is None:
        integrity = "unreadable"
    elif read.errors or read.losses or payload_issues or not read.blocks:
        integrity = "incomplete"
    else:
        integrity = "seen_range_checked"
    synchronization = "unestablished"
    if metadata.simultaneous is False or len(acquisition_ids - {None}) > 1:
        synchronization = "non_simultaneous"
    elif (
        not missing
        and not issues
        and integrity == "seen_range_checked"
        and metadata.simultaneous is True
        and len(acquisition_ids) == 1
    ):
        synchronization = "declared_same_acquisition"
    return {
        "format": "zlscan.raw-import/1",
        "source_name": Path(source).name,
        "source_sha256": digest.hexdigest(),
        "snapshot": "raw.zlraw",
        "source_kind": metadata.source_kind,
        "metadata": declaration,
        "session": session.model_dump(mode="json") if session else None,
        "integrity": integrity,
        "boundary_completeness": "unknown; no expected capture start/end supplied",
        "blocks": [asdict(b) for b in read.blocks],
        "errors": [asdict(e) for e in read.errors],
        "losses": [asdict(x) for x in read.losses],
        "sample_rows": sample_rows,
        "preview": preview,
        "preview_row_limit": 256,
        "missing_metadata": missing,
        "issues": issues,
        "synchronization": synchronization,
        "hard_acceptance_eligible": False,
        "parameter_upgrades": [],
        "interpolated": False,
        "alignment_uncertainty_s": None,
    }


def write_raw_import_report(report: dict, metadata_bytes: bytes, output: Path) -> None:
    """Publish the report only after a snapshot scan has finished."""
    report["metadata_sha256"] = hashlib.sha256(metadata_bytes).hexdigest()
    with (output / "run.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False)
