"""Local query service for the second-level workbench (S3-T16): standard-library HTTP only.

Endpoints follow docs/dev/08_workbench_spec.md section 11. Every response carries a query status
(``exact_record`` / ``replayed`` / ``summary_only`` / ``not_recorded`` / ``not_computed``) and,
where it applies, the summary level used. Query results echo the caller's ``req`` number so a
front end can drop late answers. The server reads run data and never writes it; a replay is
returned to the caller, not stored in the run. The WebSocket updates endpoint and the image tile
endpoint are not implemented: they answer ``not_computed`` with HTTP 501.

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
from zlscan.replay.replay import replay_window
from zlscan.telemetry.registry import PROBE_TO_SIGNAL, chain_signals

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


class Service:
    def __init__(self, brancher: Callable[[Run, dict], Run | None] | None = None) -> None:
        self.brancher = brancher
        self.runs: dict[str, Run] = {}
        self.lock = threading.Lock()

    def add(self, run: Run) -> None:
        with self.lock:
            self.runs[run.run_id] = run

    # ---- handlers return (http status, body) --------------------------------------------
    def manifest(self, r: Run) -> dict:
        c = r.cfg
        return {"status": QueryStatus.EXACT_RECORD, "run_id": r.run_id, "parent": r.parent,
                "session_id": r.session_id, "epoch": r.epoch, "steps": r.steps,
                "fine_step_ps": round(1e12 / (c.fs_hz * c.os)), "fs_hz": c.fs_hz, "os": c.os,
                "samples": int(len(r.codes)), "config_hash": c.hash(), "origin": "SIMULATED",
                "evidence": c.evidence, "checkpoint_interval_steps": r.store.interval_steps,
                "recorded_probes": sorted(r.recorded)}  # fmt: skip

    def signals(self, r: Run, view: str) -> dict:
        out = []
        for s in chain_signals(r.cfg):
            if view == "instrument" and s.visibility is Visibility.ORACLE_ONLY:
                continue
            d = s.model_dump(mode="json")
            d["recorded"] = s.signal_id == "adc.code" or any(
                PROBE_TO_SIGNAL[p] == s.signal_id for p in r.recorded
            )
            out.append(d)
        return {"status": QueryStatus.EXACT_RECORD, "view": view, "signals": out}

    def traces(
        self, r: Run, signal: str, start: int, end: int, pixel_width: int, view: str = "research"
    ) -> dict:
        if signal == "adc.code":
            lo, hi = max(start // r.cfg.os, 0), min(-(-end // r.cfg.os), len(r.codes))
            return {"status": QueryStatus.EXACT_RECORD, "signal": signal, "level": "raw",
                    "unit_step": "sample", "start": lo, "values": r.codes[lo:hi].tolist()}  # fmt: skip
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
        pyr = r.pyramids[probe]
        level = pyr.choose_level(end - start, pixel_width)
        if level < 0:
            return {"status": QueryStatus.NOT_RECORDED, "signal": signal,
                    "reason": "window is finer than the summaries; replay it"}  # fmt: skip
        size = pyr.block_size(level)
        lo, hi = start // size, -(-end // size)
        return {"status": QueryStatus.SUMMARY_ONLY, "signal": signal, "level": level,
                "block_steps": size, "first_block": lo,
                "columns": ["min", "max", "first", "last"],
                "values": pyr.levels[level][lo:hi].tolist()}  # fmt: skip

    def state(self, r: Run, tick_step: int, probes: set[str], view: str = "research") -> dict:
        if view == "instrument":
            return {"status": QueryStatus.NOT_RECORDED, "step": tick_step, "run_id": r.run_id,
                    "reason": "the instrument view has no access to simulation truth"}  # fmt: skip
        res = replay_window(r.cfg, r.store, tick_step, 1, probes or set(PROBE_TO_SIGNAL))
        return {"status": QueryStatus.REPLAYED, "step": tick_step, "run_id": r.run_id,
                "epoch": r.epoch, "checkpoint_step": res.checkpoint_step,
                "values": {PROBE_TO_SIGNAL[k]: float(v[0]) for k, v in res.out.probes.items()}}  # fmt: skip

    def events(
        self, r: Run, start: int, end: int, kinds: set[str] | None, view: str = "research"
    ) -> dict:
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
        return {"status": QueryStatus.EXACT_RECORD, "events": sorted(out, key=lambda e: e["step"])}

    def lineage(self, r: Run, index: int) -> dict:
        i = index - r.link.first_index
        if not 0 <= i < len(r.codes):
            return {"status": QueryStatus.NOT_RECORDED, "reason": "sample outside the run"}
        return {"status": QueryStatus.EXACT_RECORD,
                "lineage": sample_lineage(r.link, i, ["rx", "net"], None)}  # fmt: skip

    def replay(self, r: Run, start: int, n: int, probes: list[str], view: str = "research") -> dict:
        res = replay_window(r.cfg, r.store, start, n, set(probes) if view == "research" else set())
        return {"status": res.status, "checkpoint_step": res.checkpoint_step, "start": start,
                "n_steps": n, "first_sample": res.out.first_sample,
                "codes": res.out.codes.tolist(),
                "probes": {PROBE_TO_SIGNAL[k]: v.tolist() for k, v in res.out.probes.items()},
                "published": "separate; the run itself is not modified"}  # fmt: skip

    def branch(self, r: Run, overrides: dict) -> dict:
        if self.brancher is None or not overrides:
            return {"status": QueryStatus.NOT_COMPUTED, "reason": "no supported override given"}
        run = self.brancher(r, overrides)
        if run is None:
            return {"status": QueryStatus.NOT_COMPUTED, "reason": "override not supported"}
        self.add(run)
        return {"status": QueryStatus.EXACT_RECORD, "run_id": run.run_id, "parent": r.run_id}


def _handler(service: Service):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
            raw = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
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
                elif what == "traces":
                    body = service.traces(run, q["signal"], int(q["start"]), int(q["end"]),
                                          int(q.get("pixel_width", 800)), view)  # fmt: skip
                elif what == "events":
                    kinds = set(filter(None, q.get("kinds", "").split(","))) or None
                    body = service.events(run, int(q["start"]), int(q["end"]), kinds, view)
                elif what == "samples" and len(parts) > 4 and parts[4] == "lineage":
                    body = service.lineage(run, int(parts[3]))
                elif what == "images" or what == "updates":
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
    return ThreadingHTTPServer((host, port), _handler(service))
