"""Column placement from delivered ZLRAW line anchors and original source counts only."""

from dataclasses import dataclass

from zlscan.io.rawstream import FLAG_LINE_START, BlockInfo


@dataclass(frozen=True)
class Part:
    block: BlockInfo
    column_offset: int | None
    reason: str | None = None


def line_layout(blocks, channel, width):
    """Keep unplaceable parts explicit; never shift later payloads across an unknown gap."""
    rows = {}
    anchor = None
    previous_line = -1
    end = 0
    for block in blocks:
        if channel not in block.channels:
            continue
        line = block.line_number
        if line < previous_line:
            raise ValueError("capture has out-of-order line segments")
        if line != previous_line:
            anchor, end = None, 0
            previous_line = line
        parts = rows.setdefault(line, [])
        if block.flags & FLAG_LINE_START:
            if parts:
                raise ValueError("capture has repeated or late line starts")
            anchor = block
        offset, reason = None, None
        if anchor is None:
            reason = "line_start_not_delivered"
        elif block.clock_domain != anchor.clock_domain:
            reason = "different_clock_domain_from_line_start"
        else:
            candidate = block.first_sample_index - anchor.first_sample_index
            if candidate < end:
                reason = "source_counter_reset_or_overlap"
            elif candidate + block.sample_count > width:
                raise ValueError("capture source span exceeds declared line window")
            else:
                offset = candidate
                end = offset + block.sample_count
        parts.append(Part(block, offset, reason))
    return rows


def source_record(block):
    return dict(
        epoch=block.epoch,
        clock_domain=block.clock_domain,
        first_sample_index=str(block.first_sample_index),
        sample_count=block.sample_count,
        seq=block.seq,
        channels=block.channels,
        payload_offset=block.payload_offset,
        payload_crc32=block.payload_crc32,
    )
