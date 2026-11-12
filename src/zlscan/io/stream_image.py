"""Write the minimal code image and mask one line at a time from verified ZLRAW blocks."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from zlscan.contracts.flags import InvalidFlag
from zlscan.io.integrity import check_result
from zlscan.io.rawstream import FLAG_LINE_START, BlockInfo, SessionHeader, scan_blocks


def stream_code_image(
    path: Path,
    out: Path,
    lines: int,
    width: int,
    channels: tuple[int, ...],
    transport_losses: list[dict],
) -> dict:
    """Persist fixed-width rows with explicit missing-sample flags and no frame arrays."""
    missing_codes = bytes(width * len(channels) * 2)
    missing_flags = np.full(width, int(InvalidFlag.MISSING_SAMPLE), dtype="<u2").tobytes()
    next_row = 0
    incomplete_rows = 0
    with (
        (out / "codes_u16le.bin").open("xb") as codes,
        (out / "invalid_u16le.bin").open("xb") as mask,
    ):

        def fill_until(row: int) -> None:
            nonlocal next_row, incomplete_rows
            while next_row < row:
                codes.write(missing_codes)
                mask.write(missing_flags)
                next_row += 1
                incomplete_rows += 1

        def append(session: SessionHeader, block: BlockInfo, samples: np.ndarray) -> None:
            nonlocal next_row, incomplete_rows
            row, count = block.line_number, block.sample_count
            if (
                not block.flags & FLAG_LINE_START
                or not next_row <= row < lines
                or count > width
                or tuple(block.channels) != channels
                or session.windows["window_length"] != width
            ):
                raise ValueError("ZLRAW block does not match the ordered image row layout")
            fill_until(row)
            codes.write(samples.tobytes())
            codes.write(missing_codes[count * len(channels) * 2 :])
            mask.write(bytes(count * 2))
            mask.write(missing_flags[count * 2 :])
            incomplete_rows += count != width
            next_row += 1

        raw = scan_blocks(path, append)
        fill_until(lines)
    report = check_result(raw, range(lines), transport_losses)
    if incomplete_rows and report.status == "complete":
        report.status = "incomplete"
        report.notes.append("one or more delivered rows end before the declared window length")
    result = {
        "status": report.status,
        "shape": [lines, width, len(channels)],
        "channel_order": list(channels),
        "codes": "codes_u16le.bin",
        "mask_shape": [lines, width],
        "mask": "invalid_u16le.bin",
        "incomplete_rows": incomplete_rows,
    }
    (out / "code_image.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (out / "integrity.json").write_text(
        json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8"
    )
    return result
