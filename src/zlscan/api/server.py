"""Local query service for the second-level workbench (S3-T16): standard-library HTTP only.

Endpoints follow docs/dev/08_workbench_spec.md section 11. Every response carries a query status
(``exact_record`` / ``replayed`` / ``summary_only`` / ``not_recorded`` / ``not_computed``) and,
where it applies, the summary level used. Query results echo the caller's ``req`` number so a
front end can drop late answers. The server reads run data and never writes it; a replay is
returned to the caller, not stored in the run. Image tiles use registered reconstruction artifacts.
The WebSocket updates endpoint answers ``not_computed`` with HTTP 501.

fastapi was not added: AGENTS.md rule 14 asks first, and nothing here needs more than
``http.server``.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from zlscan.causality.lineage import sample_lineage
from zlscan.contracts.events import Visibility
from zlscan.contracts.query import QueryStatus
from zlscan.contracts.time import ClockDomain
from zlscan.io.image_artifact import ImageArtifact
from zlscan.replay.replay import replay_window
from zlscan.telemetry.components import COMPONENT_PROBES, component_frame
from zlscan.telemetry.registry import PROBE_TO_SIGNAL, chain_signals
from zlscan.trace.window import raw_window, summary_window, validate_window

STATIC = Path(__file__).parent / "static"


@dataclass
class Run:
    run_id: str
    cfg: Any
    steps: int
    codes: np.ndarray
    store: Any
    pyramids: dict[str, Any]
    recorded: set[str]
    events: dict[str, list[int]]
    parent: str | None = None
    link: Any = None
    losses: list[dict] = field(default_factory=list)  # transport losses: layer, first, last
    epoch: int = 0
    session_id: int = 1
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageRun:
    run_id: str
    image: ImageArtifact


class Service:
    def __init__(self, brancher: Callable[[Run, dict], Run | None] | None = None) -> None:
        self.brancher = brancher
        self.runs: dict[str, Run | ImageRun] = {}
        self.lock = threading.Lock()

    def add(self, run: Run | ImageRun) -> None:
        with self.lock:
            self.runs[run.run_id] = run

    # ---- handlers return (http status, body) --------------------------------------------
    def manifest(self, r: Run) -> dict:
        if isinstance(r, ImageRun):
            image = r.image
            return {
                "status": "exact_record",
                "run_id": r.run_id,
                "parent": None,
                "session_id": image.plan["session_id"] if image.plan else None,
                "epoch": image.epoch,
                "epochs": image.epochs,
                "steps": image.span,
                "samples": sum(row["source"]["sample_count"] for row in image.delivered),
                "cursor_unit": "sample",
                "sample_origin": str(image.first_sample),
                "steps_count": str(image.span),
                "cursor_clock": {k: str(v) for k, v in image.clocks[image.domain].__dict__.items()}
                if image.timeline_available
                else None,
                "origin": image.plan["origin"] if image.plan else "not_recorded",
                "evidence": {"source": "observable reconstruction artifact"},
                "image": image.manifest(),
                "timeline_available": image.timeline_available,
            }
        c = r.cfg
        return {"status": QueryStatus.EXACT_RECORD, "run_id": r.run_id, "parent": r.parent,
                "session_id": r.session_id, "epoch": r.epoch, "steps": r.steps,
                "fine_step_ps": round(1e12 / (c.fs_hz * c.os)), "fs_hz": c.fs_hz, "os": c.os,
                "fine_step_period_ps": {"numerator": "1000000000000", "denominator": str(c.fs_hz * c.os)},
                "samples": int(len(r.codes)), "config_hash": c.hash(), "origin": "SIMULATED",
                "component_view_available": True,
                "evidence": c.evidence, "checkpoint_interval_steps": r.store.interval_steps,
                "physical_applicability": r.meta.get("physical_applicability"),
                "recorded_probes": sorted(r.recorded),
                **({"loaded_from_saved": True, "read_only": True,
                    "artifact_integrity": r.meta["artifact_integrity"],
                    "source_adc_visibility": "ORACLE_ONLY",
                    "saved_source": r.meta["saved_source"]} if r.meta.get("saved_source") else {})}  # fmt: skip

    def signals(self, r: Run, view: str) -> dict:
        if isinstance(r, ImageRun):
            return {
                "status": "exact_record",
                "view": view,
                "signals": [
                    {
                        "signal_id": "adc.code",
                        "visibility": "OBSERVABLE",
                        "recorded": bool(r.image.raw_path and r.image.plan),
                        "unit": "code",
                        "display_interpolation": "hold",
                    }
                ],
            }
        out = []
        for s in chain_signals(r.cfg):
            if view == "instrument" and s.visibility is Visibility.ORACLE_ONLY:
                continue
            d = s.model_dump(mode="json")
            if r.meta.get("saved_source") and s.signal_id == "adc.code":
                d["visibility"] = "ORACLE_ONLY" if view == "research" else "OBSERVABLE"
            d["recorded"] = s.signal_id == "adc.code" or any(
                PROBE_TO_SIGNAL[p] == s.signal_id for p in r.recorded
            )
            out.append(d)
        return {"status": QueryStatus.EXACT_RECORD, "view": view, "signals": out}

    def traces(
        self, r: Run, signal: str, start: int, end: int, pixel_width: int, view: str = "research"
    ) -> dict:
        if isinstance(r, ImageRun):
            return (
                r.image.traces(start, end, pixel_width)
                if signal == "adc.code"
                else {
                    "status": "not_recorded",
                    "reason": "physical trace not registered for this capture",
                }
            )
        meta = getattr(r, "meta", {})
        if meta.get("validate_saved"):
            meta["validate_saved"]()
        validate_window(start, end, pixel_width)
        if meta.get("saved_source") and signal == "adc.code" and view == "instrument":
            result = meta["delivered_adc"].query(
                start // r.cfg.os, -(-end // r.cfg.os), pixel_width
            )
            if "start" in result:
                result.update(
                    first_step=result["start"] * r.cfg.os,
                    block_steps=result["block_size"] * r.cfg.os,
                    requested_window=[start, end],
                    signal=signal,
                    display_interpolation="hold",
                )
            return result
        requested = [start, end]
        start, end = max(0, start), min(end, r.steps)
        if end <= start:
            return {
                "status": QueryStatus.NOT_COMPUTED,
                "signal": signal,
                "requested_window": requested,
                "reason": "window is outside the computed run",
            }
        if signal == "adc.code":
            lo, hi = start // r.cfg.os, min(-(-end // r.cfg.os), len(r.codes))
            result = raw_window(r.codes, lo, hi, pixel_width)
            if "start" not in result:
                return {**result, "signal": signal, "requested_window": requested}
            clock = (
                r.link.domain if r.link is not None else ClockDomain(r.cfg.domain_id, r.cfg.fs_hz)
            )
            origin = r.link.first_index if r.link is not None else 0
            if meta.get("saved_source"):
                result.update(
                    visibility="ORACLE_ONLY", source="saved simulation ADC source; not delivery"
                )
            result.update(
                signal=signal,
                unit_step="sample",
                requested_window=requested,
                first_step=result["start"] * r.cfg.os,
                block_steps=result["block_size"] * r.cfg.os,
                display_interpolation="hold",
                sample_time={
                    "clock_domain": clock.id,
                    "first_sample_index": str(origin + lo),
                    "first_tick_ps": str(clock.tick_of(origin + lo)),
                    "last_tick_ps": str(clock.tick_of(origin + hi - 1)),
                    "period_ps": {
                        "numerator": str(10**12 * clock.freq_den),
                        "denominator": str(clock.freq_num),
                    },
                },
            )
            return result
        if view == "instrument":
            return {"status": QueryStatus.NOT_RECORDED, "reason": "private simulation trace"}
        probe = next((p for p, s in PROBE_TO_SIGNAL.items() if s == signal), None)
        if probe is None:
            return {
                "status": QueryStatus.NOT_COMPUTED,
                "signal": signal,
                "reason": "unknown signal",
            }
        if probe not in r.pyramids:
            return {"status": QueryStatus.NOT_RECORDED, "signal": signal,
                    "reason": "not recorded; ask for a replay-window", "recorded": False}  # fmt: skip
        result = summary_window(r.pyramids[probe], start, end, pixel_width)
        result.update(signal=signal, requested_window=requested, display_interpolation="none")
        if "start" in result:
            result.update(first_step=result["start"], block_steps=result["block_size"])
        return result

    def state(self, r: Run, tick_step: int, probes: set[str], view: str = "research") -> dict:
        if isinstance(r, ImageRun):
            return {**r.image.state(tick_step), "run_id": r.run_id}
        if view == "instrument":
            return {"status": QueryStatus.NOT_RECORDED, "step": tick_step, "run_id": r.run_id,
                    "reason": "the instrument view has no access to simulation truth"}  # fmt: skip
        if r.meta.get("validate_saved"):
            r.meta["validate_saved"]()
        if not 0 <= tick_step < r.steps:
            raise ValueError("cursor outside run")
        if not r.store.blobs:
            return {
                "status": "not_recorded",
                "reason": "saved simulation checkpoints not available",
            }
        res = replay_window(r.cfg, r.store, tick_step, 1, probes or set(PROBE_TO_SIGNAL))
        return {"status": QueryStatus.REPLAYED, "step": tick_step, "run_id": r.run_id,
                "epoch": r.epoch, "checkpoint_step": res.checkpoint_step,
                "values": {PROBE_TO_SIGNAL[k]: float(v[0]) for k, v in res.out.probes.items()}}  # fmt: skip

    def components(self, r, step, view="research"):
        if isinstance(r, ImageRun) or view == "instrument":
            return dict(status="not_recorded", reason="component model is research only")
        if r.meta.get("validate_saved"):
            r.meta["validate_saved"]()
        if type(step) is not int or not 0 <= step < r.steps:
            raise ValueError("component cursor outside run")
        if not r.store.blobs:
            return dict(status="not_recorded", reason="model checkpoints unavailable")
        start = step // r.cfg.os * r.cfg.os
        try:
            base = r.store.nearest(start)
        except LookupError:
            return dict(
                status="not_recorded", reason="model checkpoint before source edge unavailable"
            )
        if step - base + 1 > 1_000_000:
            return dict(status="not_computed", reason="component replay exceeds 1000000 fine steps")
        replay = replay_window(r.cfg, r.store, start, step - start + 1, COMPONENT_PROBES)
        values = {key: replay.out.probes[key][-1].item() for key in COMPONENT_PROBES}
        result = component_frame(
            r.cfg, step, values, int(replay.out.codes[0]), start // r.cfg.os, r.epoch
        )
        result.update(run_id=r.run_id, checkpoint_step=str(base))
        if r.meta.get("validate_saved"):
            r.meta["validate_saved"]()
        return result

    def digital_state(self, r, tick, view="research") -> dict:
        return dict(status="not_recorded", reason="original digital producer history unavailable")

    def events(
        self, r: Run, start: int, end: int, kinds: set[str] | None, view: str = "research"
    ) -> dict:
        if isinstance(r, ImageRun):
            return {
                "status": "not_recorded",
                "events": [],
                "reason": "event records not registered for this capture",
            }
        if r.meta.get("saved_source") and view == "instrument":
            return {
                "status": "not_recorded",
                "events": [],
                "reason": "event timeline not persisted",
            }
        os_ = r.cfg.os
        out = []
        for kind, idx in r.events.items() if view == "research" else ():
            if kinds and kind not in kinds:
                continue
            for i in idx:
                if start <= i * os_ < end:
                    out.append({"kind": kind, "sample": i, "step": i * os_})
        if not kinds or "transport_loss" in kinds:
            for x in r.losses:
                if start <= x["first"] * os_ < end:
                    out.append({"kind": "transport_loss", "layer": x["layer"], "first": x["first"],
                                "last": x["last"], "reason": x["reason"], "step": x["first"] * os_})  # fmt: skip
        ordered = sorted(out, key=lambda e: e["step"])
        if r.meta.get("saved_source") and (kinds is None or kinds - {"transport_loss"}):
            return {
                "status": "not_recorded",
                "events": ordered,
                "reason": "only saved simulated transport losses registered; analogue event positions not persisted",
            }
        return {"status": QueryStatus.EXACT_RECORD, "events": ordered}

    def lineage(self, r: Run, index: int, view: str = "research") -> dict:
        if isinstance(r, ImageRun):
            return {
                "status": "not_recorded",
                "reason": "output and arrival timestamps not registered",
            }
        if r.link is None:
            record = r.meta.get("saved_link_records", {}).get(index)
            if record is None or view != "research":
                return {
                    "status": "not_recorded",
                    "reason": "no observable link timestamps registered",
                }
            record = dict(record)
            for field in (
                "sample_index",
                "sample_time",
                "adc_output_time",
                "pin_arrival_time",
                "capture_time",
            ):
                record[field] = str(record[field])
            record.update(host_arrival_time=None, host_arrival_status="not_recorded")
            return {
                "status": "exact_record",
                "visibility": "ORACLE_ONLY",
                "origin": "SIMULATED",
                "source": "saved partial modeled link timestamps",
                "lineage": record,
            }
        i = index - r.link.first_index
        if not 0 <= i < len(r.codes):
            return {"status": QueryStatus.NOT_RECORDED, "reason": "sample outside the run"}
        lineage = sample_lineage(r.link, i, ["rx", "net"], None)
        for time_field in (
            "sample_index",
            "sample_time",
            "adc_output_time",
            "pin_arrival_time",
            "capture_time",
        ):
            lineage[time_field] = str(lineage[time_field])
        lineage.update(
            host_arrival_time=None,
            host_arrival_status="not_recorded",
            sample_identity={
                "run_id": r.run_id,
                "branch_id": r.run_id,
                "session_id": r.session_id,
                "epoch": r.epoch,
                "clock_domain": r.link.domain.id,
                "channel": r.link.channels[0],
                "sample_index": str(index),
            },
        )
        return {"status": QueryStatus.EXACT_RECORD, "lineage": lineage}

    def replay(self, r: Run, start: int, n: int, probes: list[str], view: str = "research") -> dict:
        if isinstance(r, ImageRun):
            return {
                "status": "not_computed",
                "reason": "this capture has no registered simulation checkpoints",
            }
        if r.meta.get("saved_source") and view == "instrument":
            return {"status": "not_computed", "reason": "source-model replay is research only"}
        if r.meta.get("validate_saved"):
            r.meta["validate_saved"]()
        if start < 0 or n < 1 or start + n > r.steps:
            raise ValueError("replay outside saved duration")
        if not r.store.blobs or min(r.store.blobs) > start:
            return {"status": "not_recorded", "reason": "simulation checkpoints not available"}
        res = replay_window(r.cfg, r.store, start, n, set(probes) if view == "research" else set())
        return {"status": res.status, "checkpoint_step": res.checkpoint_step, "start": start,
                "n_steps": n, "first_sample": res.out.first_sample,
                "codes": res.out.codes.tolist(),
                "probes": {PROBE_TO_SIGNAL[k]: v.tolist() for k, v in res.out.probes.items()},
                "published": "separate; the run itself is not modified"}  # fmt: skip

    def branch(self, r: Run, overrides: dict) -> dict:
        if isinstance(r, ImageRun):
            return {"status": "not_computed", "reason": "recorded capture is read-only"}
        if self.brancher is None or not overrides:
            return {"status": QueryStatus.NOT_COMPUTED, "reason": "no supported override given"}
        run = self.brancher(r, overrides)
        if run is None:
            return {"status": QueryStatus.NOT_COMPUTED, "reason": "override not supported"}
        self.add(run)
        return {"status": QueryStatus.EXACT_RECORD, "run_id": run.run_id, "parent": r.run_id}

    def image_query(self, run: Run | ImageRun, parts: list[str]) -> dict:
        if not isinstance(run, ImageRun):
            return {"status": "not_computed", "reason": "no reconstructed image registered"}
        if len(parts) == 3:
            return {"status": "exact_record", "images": [run.image.manifest()]}
        if parts[3] != "density":
            raise LookupError("unknown image")
        if parts[4:] == ["manifest"]:
            return run.image.manifest()
        if len(parts) == 8 and parts[4:6] == ["tiles", "0"]:
            return run.image.tile(int(parts[6]), int(parts[7]))
        if len(parts) == 8 and parts[4] == "pixels" and parts[7] == "lineage":
            return run.image.pixel_lineage(int(parts[5]), int(parts[6]))
        raise LookupError("unknown image query")


def _handler(service: Service):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            raw = (
                body
                if isinstance(body, bytes)
                else json.dumps(body, default=str, sort_keys=True).encode()
            )
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = [p for p in url.path.split("/") if p]
            req = q.get("req")
            try:
                if method == "GET" and not parts:
                    return self._send(
                        200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8"
                    )
                if parts[:1] != ["runs"]:
                    return self._send(404, {"error": "not found"})
                if len(parts) == 1:
                    return self._send(200, {"runs": sorted(service.runs)})
                run = service.runs.get(parts[1])
                if run is None:
                    return self._send(404, {"error": "unknown run"})
                what = parts[2] if len(parts) > 2 else ""
                view = q.get("view", "research")
                if view not in {"research", "instrument"}:
                    raise ValueError("unknown view")
                body: dict
                if what == "manifest":
                    body = service.manifest(run)
                elif what == "signals":
                    body = service.signals(run, view)
                elif what == "state":
                    probes = set(filter(None, q.get("probes", "").split(",")))
                    body = service.state(run, int(q["step"]), probes, view)
                elif what == "digital-state":
                    tick = int(q["tick"])
                    if str(tick) != q["tick"] or not -(2**63) <= tick < 2**63:
                        raise ValueError("tick must be a canonical signed integer ps")
                    body = service.digital_state(run, tick, view)
                elif what == "components":
                    body = service.components(run, int(q["step"]), view)
                elif what == "traces":
                    body = service.traces(run, q["signal"], int(q["start"]), int(q["end"]),
                                          int(q.get("pixel_width", 800)), view)  # fmt: skip
                elif what == "events":
                    kinds = set(filter(None, q.get("kinds", "").split(","))) or None
                    body = service.events(run, int(q["start"]), int(q["end"]), kinds, view)
                elif what == "samples" and len(parts) > 4 and parts[4] == "lineage":
                    body = service.lineage(run, int(parts[3]), view)
                elif what == "images":
                    body = service.image_query(run, parts)
                elif what == "updates":
                    body = {
                        "status": QueryStatus.NOT_COMPUTED,
                        "reason": f"{what} is not implemented",
                    }
                    body["req"] = req
                    return self._send(501, body)
                elif what == "replay-window" and method == "POST":
                    n = int(self.headers.get("Content-Length", 0))
                    p = json.loads(self.rfile.read(n) or b"{}")
                    body = service.replay(
                        run, int(p["start"]), int(p["n_steps"]), p.get("probes", []), view
                    )
                elif what == "branches" and method == "POST":
                    n = int(self.headers.get("Content-Length", 0))
                    overrides = json.loads(self.rfile.read(n) or b"{}")
                    body = service.branch(run, overrides if view == "research" else {})
                else:
                    return self._send(404, {"error": "not found"})
                body["req"] = req
                self._send(200, body)
            except (KeyError, ValueError) as exc:
                self._send(400, {"error": f"bad request: {exc}", "req": req})
            except LookupError as exc:
                self._send(404, {"error": str(exc), "req": req})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return H


def make_server(service: Service, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the query service only binds to the local machine")
    server = ThreadingHTTPServer((host, port), _handler(service))
    # Close all query workers before an enclosing saved-artifact context unmaps files.
    server.daemon_threads = False
    return server
