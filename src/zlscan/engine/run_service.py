"""Build runs for the query service (S3-T16) and start it (``zlscan serve``)."""

from __future__ import annotations

from copy import deepcopy

import numpy as np

from zlscan.adc.patterns import ConvertedSamples
from zlscan.adc_link.link import LinkConfig, transmit
from zlscan.api.server import Run, Service, make_server
from zlscan.config.presets import demo_config
from zlscan.contracts.time import ClockDomain
from zlscan.engine.chain import ChainConfig
from zlscan.replay.replay import run_with_checkpoints
from zlscan.trace.recorder import record


def build_run(run_id: str, cfg: ChainConfig, steps: int, record_probes: set[str],
              checkpoint_every: int, parent: str | None = None) -> Run:  # fmt: skip
    full, store = run_with_checkpoints(cfg, steps, checkpoint_every)
    rec = record(cfg, steps, record_probes) if record_probes else {"pyramids": {}, "events": {}}
    events = rec.get("events") or {
        "adc_saturated": np.flatnonzero(full.saturated).tolist(),
        "front_end_clipped": np.flatnonzero(full.front_end_clipped).tolist(),
        "pmt_over_limit": np.flatnonzero(full.over_limit).tolist(),
    }
    dom = ClockDomain(cfg.domain_id, cfg.fs_hz)
    src = ConvertedSamples(full.codes.reshape(-1, 1), (0,), full.first_sample, 0, dom, cfg.adc.bits)
    link = transmit(src, LinkConfig(latency_half_cycles=24, propagation_ps=1500))
    return Run(run_id, cfg, steps, full.codes, store, rec.get("pyramids", {}), set(record_probes),
               events, parent, link)  # fmt: skip


def brancher(parent: Run, overrides: dict) -> Run | None:
    cfg = deepcopy(parent.cfg)
    changed = False
    if "seed" in overrides:
        cfg.seed, changed = int(overrides["seed"]), True
    if "gain" in overrides:
        det = cfg.detector
        cfg.detector = type(det)(**{**det.__dict__, "gain": float(overrides["gain"])})
        changed = True
    if not changed:
        return None
    new_id = f"{parent.run_id}-b{abs(hash(str(sorted(overrides.items())))) % 10**6}"
    return build_run(new_id, cfg, parent.steps, set(parent.recorded),
                     parent.store.interval_steps, parent=parent.run_id)  # fmt: skip


def demo_service(lines: int = 6) -> Service:
    cfg = demo_config()
    steps = round(cfg.scan.line_period_s * cfg.fs_hz * cfg.os) * lines
    cfg.injections = [(steps // 3, 1, 2e-3)]  # a visible single-step light pulse
    svc = Service(brancher)
    probes = {"power_w", "current_a", "v_adc", "t_eff", "x_m"}
    svc.add(build_run("demo", cfg, steps, probes, steps // lines))
    return svc


def serve(port: int = 8765, lines: int = 6) -> None:
    server = make_server(demo_service(lines), "127.0.0.1", port)
    print(f"工作台服务：http://127.0.0.1:{server.server_address[1]}/  （Ctrl+C 结束）")
    try:
        server.serve_forever()
    finally:
        server.server_close()
