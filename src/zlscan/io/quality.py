"""Project capture-bound observable quality ranges onto delivered sample positions."""

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from zlscan.contracts.events import Derivation, Event
from zlscan.contracts.flags import InvalidFlag
from zlscan.contracts.verdict import Verdict
from zlscan.io.rawstream import FLAG_LINE_START, BlockInfo, ReadResult

_FLAGS = {
    "adc_saturated": InvalidFlag.ADC_SATURATED,
    "front_end_clipped": InvalidFlag.FRONT_END_CLIPPED,
    "pmt_over_limit": InvalidFlag.FRONT_END_CLIPPED,
    "transport_loss": InvalidFlag.MISSING_SAMPLE,
}


class QualityRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Event
    samples: StrictInt = Field(default=1, gt=0)


class QualityLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_sha256: str
    ranges: list[QualityRange]


def quality_spans(
    path: Path,
    raw_sha256: str,
    raw: ReadResult,
    channel: int,
    lines: int,
    width: int,
) -> Iterator[tuple[BlockInfo, int, int, int]]:
    """Yield validated quality spans as half-open column indices in acquired rows."""
    log = QualityLog.model_validate_json(path.read_bytes())
    session = raw.session
    if session is None or log.raw_sha256 != raw_sha256:
        raise ValueError("quality events do not belong to this raw capture")
    ranges: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
    domains = {clock["id"] for clock in session.clock_domains}
    for item in log.ranges:
        event = item.event
        sample = event.related_sample
        if (
            event.kind not in _FLAGS
            or event.recon_verdict() is not Verdict.PASS
            or event.derivation is Derivation.COMMAND
            or event.origin.value != session.origin
            or event.session_id != session.session_id
            or event.clock_domain not in domains
            or sample is None
            or sample.session_id != event.session_id
            or sample.epoch != event.epoch
            or sample.clock_domain != event.clock_domain
            or str(sample.channel) not in session.channel_roles
            or sample.sample_index < 0
        ):
            raise ValueError("invalid or unobservable quality event for this capture")
        if sample.channel == channel:
            first = sample.sample_index
            ranges.setdefault((sample.epoch, sample.clock_domain), []).append(
                (first, first + item.samples, int(_FLAGS[event.kind]))
            )

    pending = {key: iter(sorted(values)) for key, values in ranges.items()}
    following = {key: next(iterator, None) for key, iterator in pending.items()}
    active: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
    for block in raw.blocks:
        if (
            channel not in block.channels
            or not block.flags & FLAG_LINE_START
            or not 0 <= block.line_number < lines
        ):
            continue
        first = block.first_sample_index
        end = first + min(block.sample_count, width)
        key = (block.epoch, block.clock_domain)
        if key not in pending:
            continue
        active[key] = [span for span in active[key] if span[1] > first]
        entry = following[key]
        while entry is not None and entry[0] < end:
            if entry[1] > first:
                active[key].append(entry)
            entry = next(pending[key], None)
        following[key] = entry
        for lo, hi, reason in active[key]:
            start, stop = max(lo, first), min(hi, end)
            if start < stop:
                yield block, start - first, stop - first, reason


def apply_quality_log(
    path: Path, raw_sha256: str, raw: ReadResult, channel: int, flags: np.ndarray
) -> None:
    """Set sample flags in place, preserving any loss already detected by the raw reader."""
    for block, start, stop, reason in quality_spans(path, raw_sha256, raw, channel, *flags.shape):
        flags[block.line_number, start:stop] |= reason
