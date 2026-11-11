"""Integrity report for a raw file (S2-T05): blocks, sample continuity, CRC, epochs, lines."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from zlscan.io.rawstream import ReadResult, read_all


@dataclass
class IntegrityReport:
    status: str  # "complete" | "incomplete" | "unreadable"
    blocks: int = 0
    lines_present: list[int] = field(default_factory=list)
    lines_missing: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    losses: list[dict] = field(default_factory=list)  # reader-inferred and transport records
    epochs: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_result(
    read: ReadResult,
    expected_lines: range | None = None,
    transport_losses: list[dict] | None = None,
) -> IntegrityReport:
    if read.fatal or read.session is None:
        return IntegrityReport("unreadable", errors=[f"{e.code}: {e.message}" for e in read.errors])
    present = sorted({b.line_number for b in read.blocks})
    rep = IntegrityReport(
        "complete",
        blocks=len(read.blocks),
        lines_present=present,
        errors=[f"{e.code}: {e.message}" for e in read.errors],
        losses=[
            {
                "layer": x.layer,
                "first": x.first_sample_index,
                "last": x.last_sample_index,
                "reason": x.reason,
                "certainty": x.certainty,
                "source": "reader",
            }
            for x in read.losses
        ]  # fmt: skip
        + [dict(x, source="transport") for x in (transport_losses or [])],
        epochs=sorted({b.epoch for b in read.blocks}),
    )
    if expected_lines is not None:
        have = set(present)
        rep.lines_missing = [n for n in expected_lines if n not in have]
    else:
        span = range(present[0], present[-1] + 1) if present else range(0)
        rep.lines_missing = [n for n in span if n not in set(present)]
        rep.notes.append("no expected line range given: only gaps inside the range seen are known")
    if rep.errors or rep.losses or rep.lines_missing:
        rep.status = "incomplete"
    return rep


def check_file(
    path: Path, expected_lines: range | None = None, losses_path: Path | None = None
) -> IntegrityReport:
    extra = None
    if losses_path is not None and Path(losses_path).exists():
        extra = json.loads(Path(losses_path).read_text(encoding="utf-8"))
    return check_result(read_all(path), expected_lines, extra)
