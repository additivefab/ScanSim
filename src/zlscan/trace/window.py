"""Bounded display windows; summaries are never reconstruction or calibration input."""

from __future__ import annotations

import numpy as np

from zlscan.trace.pyramid import Pyramid

MAX_PIXEL_WIDTH = 4096


def validate_window(start: int, end: int, pixel_width: int) -> None:
    if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end, pixel_width)):
        raise ValueError("query bounds and pixel_width must be integers")
    if end <= start or not 1 <= pixel_width <= MAX_PIXEL_WIDTH:
        raise ValueError("query needs start < end and pixel_width in 1..4096")


def raw_window(values: np.ndarray, start: int, end: int, pixel_width: int) -> dict:
    validate_window(start, end, pixel_width)
    values = np.asarray(values)
    if values.ndim != 1:
        raise ValueError("raw display trace must be one-dimensional")
    start, end = max(0, start), min(len(values), end)
    if end <= start:
        return {"status": "not_recorded", "values": [], "reason": "outside recorded samples"}
    count = end - start
    block = 1 if count <= 2 * pixel_width else -(-count // pixel_width)
    metadata = {
        "start": start,
        "block_size": block,
        "support_start": start,
        "support_end": end,
        "source_count": count,
        "boundary_extra": False,
    }
    if block == 1:
        return {
            **metadata,
            "status": "exact_record",
            "level": "raw",
            "values": values[start:end].tolist(),
        }
    rows = []
    for first in range(start, end, block):
        chunk = values[first : min(first + block, end)]
        rows.append(
            [chunk.min().item(), chunk.max().item(), chunk[0].item(), chunk[-1].item(), len(chunk)]
        )
    return {
        **metadata,
        "status": "summary_only",
        "level": "display_bins",
        "values": rows,
        "columns": ["min", "max", "first", "last", "count"],
    }


def summary_window(pyramid: Pyramid, start: int, end: int, pixel_width: int) -> dict:
    validate_window(start, end, pixel_width)
    start, end = max(0, start), min(pyramid.length, end)
    if end <= start:
        return {"status": "not_recorded", "reason": "outside recorded summaries"}
    level = pyramid.choose_level(end - start, pixel_width)
    if level < 0:
        return {
            "status": "not_recorded",
            "reason": "window is finer than recorded summaries; replay it",
        }
    size = pyramid.block_size(level)
    lo, hi = start // size, -(-end // size)
    records = pyramid.levels[level][lo:hi]
    merge = max(1, -(-len(records) // pixel_width))
    rows = []
    for index in range(0, len(records), merge):
        group = records[index : index + merge]
        first = (lo + index) * size
        stop = min(pyramid.length, first + len(group) * size)
        rows.append(
            [
                float(group[:, 0].min()),
                float(group[:, 1].max()),
                float(group[0, 2]),
                float(group[-1, 3]),
                stop - first,
            ]
        )
    support_start, support_end = lo * size, min(hi * size, pyramid.length)
    return {
        "status": "summary_only",
        "level": level,
        "aggregation_factor": merge,
        "start": support_start,
        "block_size": size * merge,
        "values": rows,
        "columns": ["min", "max", "first", "last", "count"],
        "source_count": support_end - support_start,
        "support_start": support_start,
        "support_end": support_end,
        "boundary_extra": support_start < start or support_end > end,
    }
