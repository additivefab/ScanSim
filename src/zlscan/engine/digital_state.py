"""Bounded, read-only projection of original digital inputs onto an inclusive integer tick."""

from __future__ import annotations

import hashlib
import json

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.engine.digital_pipeline import DigitalPipeline
from zlscan.engine.physical_recording import PhysicalRecorder
from zlscan.io.producer_history import describe_source

MAX_REPLAY_STEPS = 1_000_000


def replay_state(saved, tick):
    fine = saved.current.fine_clock
    if (
        type(tick) is not int
        or not -(2**63) <= tick < 2**63
        or not fine.tick_of(0) <= tick < fine.tick_of(saved.step)
    ):
        raise ValueError("digital tick outside committed source timeline or signed integer ps")
    if saved.history_reader is None:
        return dict(status="not_recorded", reason="original digital producer history unavailable")
    saved.validate_prefixes()
    try:
        restored, name = saved._nearest(fine.count_at(tick), need_history=True)
    except ValueError as exc:
        if str(exc).startswith("no recorded checkpoint"):
            return dict(status="not_recorded", reason=str(exc))
        raise
    path = saved.directory / "history" / name
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    if not name.endswith(digest + ".json"):
        raise ValueError("recording history changed during digital query")
    root = json.loads(blob)
    record = root["recording"]
    pipeline = DigitalPipeline.restore(
        restored.domain,
        saved.link,
        saved.acquisition,
        saved.stages,
        restored.channels,
        json.dumps(record["pipeline"]).encode(),
    )
    if pipeline.watermark is not None and pipeline.watermark > tick:
        raise ValueError("historical digital watermark exceeds query tick")
    counts = {key: int(record[key]) for key in ("delivered", "lost", "retained_samples")}

    def consume():
        batch = pipeline.drain()
        counts["delivered"] += len(batch.delivered)
        counts["lost"] += len(batch.losses)
        counts["retained_samples"] += sum(len(p.packet.source) for p in batch.delivered)

    operations = groups = work = 0
    cursor = restored.applied_commands
    plan = saved.current.commands
    for row in saved.history_reader.after(root["digital_history"]):
        start = int(row["step"])
        if fine.tick_of(start) > tick:
            break
        kind = row["op"]
        if kind == "sync":
            pipeline.schedule_syncs([int(t) for t in row["ticks"]])
        elif kind == "advance":
            end = int(row["end"])
            if start != restored.step:
                raise ValueError("producer operation and physical replay boundary differ")
            work += end - start
            if work > MAX_REPLAY_STEPS:
                return dict(
                    status="not_computed", reason="digital replay exceeds 1000000 fine steps"
                )
            while cursor < len(plan) and int(plan[cursor]["effective_step"]) <= start:
                command = plan[cursor]
                PhysicalRecorder._apply_config(restored.chains[command["channel"]].cfg, command)
                cursor += 1
            if cursor < len(plan) and int(plan[cursor]["effective_step"]) < end:
                raise ValueError("original producer batch crosses a physical control change")
            outputs = [chain.advance(end - start) for chain in restored.chains.values()]
            first = outputs[0].first_sample
            codes = np.column_stack([out.codes for out in outputs])
            parts = []
            for part in row["sources"]:
                begin, count = int(part["first_index"]), part["count"]
                source = ConvertedSamples(
                    codes[begin - first : begin - first + count],
                    restored.channels,
                    begin,
                    part["epoch"],
                    restored.domain,
                    saved.link.bits,
                    **restored.identity,
                )
                if describe_source(source) != part or any(
                    saved.current.controller.epoch_at(restored.domain.tick_of(i)).id
                    != part["epoch"]
                    for i in (begin, begin + count - 1)
                ):
                    raise ValueError("original producer source code fingerprint or epoch differs")
                parts.append(source)
                groups += 1
            partial = False
            for source in parts:
                if source.sample_time(len(source) - 1) <= tick and not partial:
                    pipeline.push(source)
                    consume()
                else:
                    # Preserve the original submitted source group. Future rows are explicitly
                    # prefetched, not counted as samples already taken at the cursor.
                    pipeline.link.push(source)
                    partial = True
            if partial:
                break
        elif kind == "finish":
            # EOF drains eagerly; no historical projection after the source interval is claimed.
            raise ValueError("EOF digital state is outside the supported source timeline")
        if hashlib.sha256(pipeline.checkpoint()).hexdigest() != row["after"]:
            raise ValueError("original producer digital state fingerprint differs")
        operations += 1
        if kind == "advance" and fine.tick_of(int(row["end"])) > tick:
            break
    pipeline.advance_to(tick)
    consume()
    result = summarize(saved, pipeline, tick, counts)
    result["replay"] = dict(
        checkpoint_step=str(int(root["digital_history"]["step"])),
        checkpoint=name,
        verified_operations=operations,
        verified_source_groups=groups,
        recomputed_fine_steps=work,
        source_grouping="original",
        source_root_sha256=hashlib.sha256(saved.root_blob).hexdigest(),
    )
    saved.validate_prefixes()
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("recording history changed during digital query")
    return result


def summarize(saved, pipeline, tick, counts):
    inflight = future = 0
    for pending in pipeline.link.pending:
        row = pending.output
        inflight += int(np.count_nonzero((row.sample_time <= tick) & (row.capture_time > tick)))
        future += int(np.count_nonzero(row.sample_time > tick))
    acquisition = pipeline.acquisition
    window = acquisition.active
    active = (
        None
        if window is None
        else dict(
            line_number=str(window.line_number),
            sync_tick_ps=str(window.sync_tick),
            counter_zero_index=str(window.counter_zero_index),
            capture_start_index=str(window.capture_start_index),
            capture_end_index=str(window.capture_end_index),
            retained_rows=str(sum(len(s) for s in window.segments)),
            epochs=sorted({s.epoch for s in window.segments}),
            clock_basis=window.clock_basis,
            outside_initial=window.outside_initial,
        )
    )
    control = "unconfigured"
    for entry in saved.current.controller.log:
        if entry.tick <= tick:
            if entry.kind == "state":
                control = entry.detail["to"]
            elif entry.kind == "reset":
                control = "unconfigured"
    domain = saved.current.domain
    total = -(-saved.step // next(iter(saved.configs.values())).os)
    sampled = max(0, min(total, domain.count_at(tick) + 1))
    return dict(
        status="replayed",
        origin="SIMULATED",
        visibility="ORACLE_ONLY",
        tick_ps=str(tick),
        closure="inclusive",
        assumption="original configured digital timing and queue model",
        source=dict(
            sampled_rows=str(sampled),
            last_sample_index=str(sampled - 1) if sampled else None,
            clock_domain=domain.id,
        ),
        link=dict(
            in_flight_rows=str(inflight),
            prefetched_future_rows=str(future),
            declared_next_index=str(pipeline.link.next_index)
            if pipeline.link.next_index is not None
            else None,
            capture_count=str(pipeline.capture_clock.count_at(tick)),
        ),
        acquisition=dict(
            active=active,
            scheduled_syncs=str(len(acquisition.events)),
            next_line_number=str(acquisition.next_line_number),
            busy_until_capture_count=str(acquisition.busy_until),
            received_next_source_index=str(acquisition.next_source_index)
            if acquisition.next_source_index is not None
            else None,
        ),
        control=dict(state=control, epoch=saved.current.controller.epoch_at(tick).id),
        transport=dict(
            stages=[
                dict(
                    name=s.spec.name,
                    resident_bytes=str(s.bytes),
                    capacity_bytes=str(s.spec.capacity_bytes)
                    if s.spec.capacity_bytes is not None
                    else None,
                    rate_Bps=s.spec.rate_Bps,
                    queued_packets=str(len(s.queue)),
                    serving_packet_id=str(s.serving.item_id) if s.serving else None,
                    blocked_packet_id=str(s.blocked.item_id) if s.blocked else None,
                    overflow_count=str(s.stats.overflow_count),
                    peak_bytes=str(s.stats.peak_bytes),
                )
                for s in pipeline.transport.stages
            ]
        ),
        counts=dict(
            delivered_packets=str(counts["delivered"]),
            lost_packets=str(counts["lost"]),
            retained_rows=str(counts["retained_samples"]),
        ),
    )
