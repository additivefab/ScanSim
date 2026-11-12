"""Frozen recording channels for delivered-only and control-aware research queries."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
from pathlib import Path

import numpy as np

from zlscan.api.server import Run, Service
from zlscan.contracts.time import CaptureClock
from zlscan.engine.recording_replay import MAX_WINDOW_STEPS, RecordingReplay
from zlscan.engine.validity import physical_applicability
from zlscan.io.committed_adc import CommittedADC
from zlscan.io.model_quality import GRIDS
from zlscan.replay.replay import CheckpointStore
from zlscan.telemetry.registry import PROBE_TO_SIGNAL, chain_signals
from zlscan.trace.window import raw_window, validate_window


class RecordingService(Service):
    def __init__(self, snapshot: RecordingReplay, delivered: CommittedADC):
        super().__init__()
        self.snapshot, self.delivered = snapshot, delivered
        self.probes = snapshot.trace_reader
        self.quality = snapshot.quality_reader
        for channel, cfg in snapshot.configs.items():
            self.add(
                Run(
                    f"recording-c{channel}",
                    cfg,
                    snapshot.step,
                    np.empty(0, dtype=np.uint16),
                    CheckpointStore(0),
                    {},
                    set(),
                    {},
                    session_id=snapshot.spec.session_id,
                    meta={"channel": channel},
                )
            )

    def _check(self):
        return self.snapshot.validate_prefixes()

    def manifest(self, r):
        tail = self._check()
        cfg = r.cfg
        channel = r.meta["channel"]
        available = 0 < r.steps <= 2**53 - 1
        return dict(
            status="exact_record",
            run_id=r.run_id,
            parent=None,
            session_id=r.session_id,
            epoch=None,
            epochs=self.delivered.epochs,
            channel=channel,
            steps=r.steps,
            steps_count=str(r.steps),
            samples=-(-r.steps // cfg.os),
            delivered_samples_per_channel=self.delivered.delivered_rows,
            cursor_unit="fine_step",
            fine_step_period_ps={"numerator": str(10**12), "denominator": str(cfg.fs_hz * cfg.os)},
            fs_hz=cfg.fs_hz,
            os=cfg.os,
            origin="SIMULATED",
            evidence=cfg.evidence,
            config_hash=cfg.hash(),
            checkpoint_interval_steps=None,
            recorded_probes=list(self.probes.policy.probes) if self.probes is not None else [],
            recorded_model_event_grids=dict(GRIDS) if self.quality is not None else {},
            physical_applicability=physical_applicability(
                self.snapshot.current.chains[channel].cfg
            ),
            loaded_from_saved=True,
            read_only=True,
            frozen_commit=True,
            live_follow=False,
            saved_source=str(self.snapshot.directory),
            artifact_integrity="committed_prefix_verified",
            source_root_sha256=hashlib.sha256(self.snapshot.root_blob).hexdigest(),
            source_adc_visibility="ORACLE_ONLY",
            uncommitted_tail_present=tail,
            timeline_available=available,
            timeline_reason=""
            if available
            else (
                "尚无已提交的模型细步"
                if r.steps == 0
                else "时间轴超出浏览器精确整数范围；请使用精确 API 查询"
            ),
        )

    def signals(self, r, view):
        self._check()
        output = []
        for signal in chain_signals(r.cfg):
            row = signal.model_dump(mode="json")
            if view == "instrument" and row["visibility"] == "ORACLE_ONLY":
                continue
            row.update(
                recorded=view == "instrument" and row["signal_id"] == "adc.code",
                replay_available=view == "research",
            )
            if view == "research":
                row["visibility"] = "ORACLE_ONLY"
                persisted = self.probes is not None and row["signal_id"] in {
                    PROBE_TO_SIGNAL[p] for p in self.probes.policy.probes
                }
                row["recorded"] = persisted
                row["recording_policy"] = (
                    ("full" if self.probes.policy.keep_raw else "summary")
                    if persisted
                    else "on_demand"
                )
            output.append(row)
        return dict(status="exact_record", signals=output, view=view)

    def _model(self, start, n, probes):
        if n > MAX_WINDOW_STEPS:
            return dict(
                status="not_computed", reason="model replay exceeds 1000000 fine steps; zoom in"
            )
        try:
            return self.snapshot.replay(start, n, set(probes))
        except ValueError as exc:
            if str(exc).startswith("no recorded checkpoint"):
                return dict(status="not_recorded", reason=str(exc))
            raise

    def traces(self, r, signal, start, end, pixel_width, view="research"):
        validate_window(start, end, pixel_width)
        self._check()
        requested = [start, end]
        start, end = max(0, start), min(r.steps, end)
        if end <= start:
            return dict(status="not_recorded", values=[], reason="outside committed timeline")
        if view == "instrument":
            if signal != "adc.code":
                return dict(status="not_recorded", reason="internal probes are not observable")
            lo, hi = -(-start // r.cfg.os), -(-end // r.cfg.os)
            if lo == hi:
                return dict(status="not_recorded", values=[], reason="no ADC edge in window")
            result = self.delivered.query(r.meta["channel"], lo, hi, pixel_width)
            result.update(
                first_step=result["start"] * r.cfg.os, block_steps=result["block_size"] * r.cfg.os
            )
        else:
            inverse = {v: k for k, v in PROBE_TO_SIGNAL.items()}
            if signal != "adc.code" and signal not in inverse:
                raise ValueError("unknown signal")
            probes = {inverse[signal]} if signal != "adc.code" else set()
            if (
                self.probes is not None
                and signal != "adc.code"
                and inverse[signal] in self.probes.policy.probes
            ):
                persisted = self.probes.query(
                    r.meta["channel"], inverse[signal], start, end, pixel_width
                )
                if persisted["status"] != "not_recorded":
                    persisted.update(signal=signal, requested_window=requested)
                    self._check()
                    return persisted
            result = self._model(start, end - start, probes)
            if "metadata" not in result:
                return result
            col = list(self.snapshot.current.channels).index(r.meta["channel"])
            values = (
                result["codes"][:, col] if signal == "adc.code" else result[inverse[signal]][:, col]
            )
            first = (
                result["metadata"]["first_sample_index"] * r.cfg.os
                if signal == "adc.code"
                else start
            )
            stride = r.cfg.os if signal == "adc.code" else 1
            display = (
                raw_window(values, 0, len(values), pixel_width)
                if len(values)
                else dict(status="not_recorded", values=[], reason="no source ADC edge in window")
            )
            if "start" in display:
                display.update(
                    first_step=first,
                    block_steps=display["block_size"] * stride,
                    status="replayed" if display["level"] == "raw" else "summary_only",
                    visibility="ORACLE_ONLY",
                    derivation="replayed",
                    checkpoint_step=result["metadata"]["checkpoint_step"],
                )
            result = display
        result.update(
            signal=signal,
            requested_window=requested,
            display_interpolation="hold" if signal == "adc.code" else "none",
        )
        self._check()
        return result

    def state(self, r, tick_step, probes, view="research"):
        self._check()
        if view == "instrument":
            return dict(
                status="not_recorded", reason="instrument view cannot replay internal state"
            )
        if not 0 <= tick_step < r.steps:
            raise ValueError("cursor outside committed recording")
        wanted = set(probes) or set(PROBE_TO_SIGNAL)
        if wanted - set(PROBE_TO_SIGNAL):
            raise ValueError("unknown registered probe")
        result = self._model(tick_step, 1, wanted)
        if "metadata" not in result:
            return result
        col = list(self.snapshot.current.channels).index(r.meta["channel"])
        return dict(
            status="replayed",
            visibility="ORACLE_ONLY",
            step=tick_step,
            run_id=r.run_id,
            checkpoint_step=result["metadata"]["checkpoint_step"],
            values={PROBE_TO_SIGNAL[p]: float(result[p][0, col]) for p in wanted},
        )

    def replay(self, r, start, n, probes, view="research"):
        self._check()
        if view == "instrument":
            return dict(status="not_computed", reason="source model replay is research only")
        if start < 0 or n < 1 or start + n > r.steps:
            raise ValueError("replay outside committed timeline")
        if set(probes) - set(PROBE_TO_SIGNAL):
            raise ValueError("unknown registered probe")
        result = self._model(start, n, probes)
        if "metadata" not in result:
            return result
        col = list(self.snapshot.current.channels).index(r.meta["channel"])
        meta = result["metadata"]
        return dict(
            status="replayed",
            visibility="ORACLE_ONLY",
            checkpoint_step=meta["checkpoint_step"],
            start=start,
            n_steps=n,
            first_sample=meta["first_sample_index"],
            first_sample_index=str(meta["first_sample_index"]),
            codes=result["codes"][:, col].tolist(),
            epochs=result["epochs"].tolist(),
            probes={PROBE_TO_SIGNAL[p]: result[p][:, col].tolist() for p in probes},
            published="separate research result; source unchanged",
            source_root_sha256=meta["source_root_sha256"],
        )

    def events(self, r, start, end, kinds, view="research"):
        validate_window(start, end, 1)
        self._check()
        known = {"epoch_observed"}
        output = []
        truncated = False
        quality_support = None
        quality_incomplete = False
        if kinds is None or "epoch_observed" in kinds:
            for event in self.delivered.events(
                r.meta["channel"],
                max(0, -(-start // r.cfg.os)),
                min(self.delivered.length, -(-end // r.cfg.os)),
                limit=4097,
            ):
                output.append({**event, "step": int(event["sample_index"]) * r.cfg.os})
        if view == "research":
            if self.quality is not None:
                selected = set(GRIDS) if kinds is None else set(kinds) & set(GRIDS)
                if selected:
                    quality = self.quality.query(
                        r.meta["channel"], start, end, selected, limit=4097
                    )
                    quality_support = {
                        key: quality.get(key)
                        for key in [
                            "support_start",
                            "support_end",
                            "requested_window",
                            "clipped_window",
                            "grids",
                        ]
                    }
                    quality_incomplete = quality["status"] != "exact_record"
                    output.extend(quality["events"])
                    truncated = quality.get("truncated", False)
                known.update(GRIDS)
            known.update(
                {"control_request", "control_effective", "control_readback", "transport_loss"}
            )
            for command in self.snapshot.current.commands:
                for kind, key in [
                    ("control_request", "request_step"),
                    ("control_effective", "effective_step"),
                    ("control_readback", "readback_step"),
                ]:
                    step = command[key]
                    if (
                        step is not None
                        and start <= int(step) < end
                        and (kinds is None or kind in kinds)
                    ):
                        output.append(
                            dict(
                                kind=kind,
                                step=int(step),
                                channel=command["channel"],
                                parameter=command["parameter"],
                                value=command["readback_value"]
                                if kind == "control_readback"
                                else command["value"],
                                visibility="ORACLE_ONLY",
                            )
                        )
            if kinds is None or "transport_loss" in kinds:
                for loss in self.delivered.losses(
                    max(0, start // r.cfg.os),
                    min(self.delivered.length, -(-end // r.cfg.os)),
                    limit=4097,
                ):
                    first, last = int(loss["first_sample_index"]), int(loss["last_sample_index"])
                    output.append(
                        dict(
                            kind="transport_loss",
                            step=first * r.cfg.os,
                            first=str(first),
                            last=str(last),
                            layer=loss["layer"],
                            reason=loss["reason"],
                            visibility="ORACLE_ONLY",
                        )
                    )
        output.sort(key=lambda e: e["step"])
        partial = kinds is None or bool(kinds - known) or quality_incomplete
        return dict(
            status="not_recorded" if partial else "exact_record",
            events=output[:4096],
            truncated=truncated or len(output) > 4096,
            **({"quality_support": quality_support} if quality_support is not None else {}),
            reason="unregistered or out-of-span events not persisted; supplied records are partial"
            if partial
            else "",
        )

    def lineage(self, r, index, view="research"):
        self._check()
        if not 0 <= index < self.delivered.length:
            raise ValueError("sample outside committed source span")
        record = self.delivered.sample(r.meta["channel"], index)
        if record is None:
            return dict(
                status="not_recorded", reason="sample was not delivered in this frozen prefix"
            )
        domain = self.snapshot.current.domain
        line = {
            **record,
            "sample_index": str(index),
            "channel": r.meta["channel"],
            "sample_time": str(domain.tick_of(index)),
            "adc_output_time": None,
            "pin_arrival_time": None,
            "capture_time": None,
            "host_arrival_time": None,
        }
        observed = self.delivered.observables(
            record["block_sequence"], index - int(record["first_sample_index"])
        )
        for name, value in observed.get("values", {}).items():
            line[name] = value if isinstance(value, bool) else str(value)
        response = dict(
            status="exact_record",
            visibility="OBSERVABLE",
            lineage=line,
            time_provenance={
                "sample_time": "derived from stored clock definition",
                "other_times": "not_recorded",
            },
            source="committed raw block",
        )
        response["observable_status"] = observed["status"]
        if line["capture_time"] is not None:
            response["time_provenance"]["capture_time"] = "original recorded observable"
        if view == "research":
            link = self.snapshot.link
            period = domain._numerator
            output = domain.phase_ticks + ((2 * index + link.latency_half_cycles) * period) // (
                2 * domain.freq_num
            )
            pin = output + link.propagation_ps
            capture = CaptureClock(domain, link.capture_edge, link.dco_phase_ps)
            response["model_lineage"] = dict(
                visibility="ORACLE_ONLY",
                derivation="MODEL_ESTIMATE",
                sample_time=str(domain.tick_of(index)),
                adc_output_time=str(output),
                pin_arrival_time=str(pin),
                capture_time=str(capture.tick_of(capture.first_at(pin))),
                host_arrival_time=None,
                reason="original assumed link equations; not recorded host arrival",
            )
            response["transport_diagnostic"] = dict(
                visibility="ORACLE_ONLY",
                last_stage_completion=self.delivered.delivery(record["block_sequence"]),
                host_arrival_time=None,
                source="original simulation journal; not host receipt",
            )
        self._check()
        return response


@contextmanager
def recording_service(directory: Path):
    with ExitStack() as stack:
        snapshot = stack.enter_context(RecordingReplay(directory))
        delivered = stack.enter_context(
            CommittedADC(
                directory / "raw.zlraw",
                snapshot.record["writer"],
                -(-snapshot.step // next(iter(snapshot.configs.values())).os),
                tuple(snapshot.current.channels),
                snapshot.current.domain.id,
            )
        )
        if delivered.delivered_rows != int(
            snapshot.record["retained_samples"]
        ) or delivered.block_count != int(snapshot.record["delivered"]):
            raise ValueError("recording raw index and committed counters differ")
        delivered.add_journal(directory / "events.jsonl", snapshot.journal_cursor)
        snapshot.validate_prefixes()
        yield RecordingService(snapshot, delivered)
