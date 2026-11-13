"""``zlscan run --mode digital``: ADC test codes -> link -> acquisition -> queues -> raw file ->
integrity -> minimal reconstruction -> first-level workbench (S2-T10).

The run directory keeps what a real run could see (``observable/``) apart from what only the
simulation knows (``truth/``). Reconstruction reads a *copy* of ``observable/`` made in a fresh
directory, so it cannot reach the truth files.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from zlscan.adc import patterns
from zlscan.adc_link.link import LinkConfig, receive, transmit
from zlscan.contracts.time import ClockDomain
from zlscan.digital.acquisition import AcquisitionConfig, acquire, block_args, session_header
from zlscan.evidence.table import load_parameters
from zlscan.io.integrity import check_file
from zlscan.io.rawstream import RawWriter
from zlscan.recon.minimal import reconstruct
from zlscan.report.viewer import build_viewer
from zlscan.transport.queues import Item, StageSpec, simulate

DEFAULTS: dict[str, Any] = {
    "adc_hz": 50_000_000,
    "lines": 12,
    "line_cycles": 200,
    "window_start": 10,
    "window_length": 40,
    "sync_latency_cycles": 2,
    "seed": 11,
    "latency_half_cycles": 24,
    "propagation_ps": 1500,
    "stages": [
        {"name": "rx", "capacity_bytes": 4096, "rate_Bps": 1e9},
        {"name": "net", "capacity_bytes": 65536, "rate_Bps": 1e8, "stalls": []},
    ],
}


def run_digital(out: Path, config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = {**DEFAULTS, **(config or {})}
    dom = ClockDomain(id=1, freq_num=int(cfg["adc_hz"]))
    n = cfg["line_cycles"] * (cfg["lines"] + 2)
    src = patterns.make_source({0: patterns.ramp(100, 3, n), 1: patterns.prbs(cfg["seed"], n)}, dom)
    line_ticks = dom.tick_of(cfg["line_cycles"])
    syncs = [7_000 + i * line_ticks for i in range(cfg["lines"])]
    return run_from_source(out, src, syncs, cfg, preset="digital_default")


def run_from_source(out: Path, src, syncs: list[int], cfg: dict[str, Any], preset: str,
                    extra_run: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    out = Path(out)
    obs, truth = out / "observable", out / "truth"
    obs.mkdir(parents=True, exist_ok=True)
    truth.mkdir(parents=True, exist_ok=True)
    dom = src.domain
    link = LinkConfig(latency_half_cycles=cfg.get("latency_half_cycles", 24),
                      propagation_ps=cfg.get("propagation_ps", 1500))  # fmt: skip
    tx = transmit(src, link)
    got = receive(tx, link)
    acq = AcquisitionConfig(
        cfg["window_start"], cfg["window_length"], cfg.get("sync_latency_cycles", 2)
    )
    res = acquire(got, syncs, acq)

    items = [
        Item(i, line.samples.size * 2 + 64, got.epoch, dom.id, line.start_index, len(line.samples),
             dom.tick_of(line.start_index + len(line.samples)), line.line_number)
        for i, line in enumerate(res.lines)
    ]  # fmt: skip
    stages = [
        StageSpec(
            s["name"], s["capacity_bytes"], s["rate_Bps"],
            tuple(map(tuple, s.get("stalls", []))), s.get("backpressure", True),
            tuple(map(tuple, s.get("fail_windows", []))),
        )
        for s in cfg["stages"]
    ]  # fmt: skip
    tr = simulate(items, stages)
    delivered = {it.item_id for it, _ in tr.delivered}

    raw = obs / "raw.zlraw"
    with RawWriter(raw, session_header(got, acq, 1)) as w:
        written = 0
        for i, line in enumerate(res.lines):
            if i in delivered:
                w.write_block(**block_args(got, line, first=written == 0))
                written += 1
    (obs / "losses.json").write_text(
        json.dumps([asdict(x) for x in tr.losses], indent=1), encoding="utf-8"
    )
    expected = range(0, len(syncs))
    report = check_file(raw, expected, obs / "losses.json")
    (obs / "integrity.json").write_text(json.dumps(asdict(report), indent=1), encoding="utf-8")

    np.save(truth / "source_codes.npy", src.codes)  # simulation truth, not visible to a real run

    with tempfile.TemporaryDirectory(prefix="zlscan_isolated_") as iso:
        shutil.copytree(obs, Path(iso) / "observable")
        img = reconstruct(Path(iso) / "observable" / "raw.zlraw", expected)
    np.savez(obs / "recon.npz", invalid=img.invalid, **{f"ch{c}": v for c, v in img.codes.items()})

    run = {
        "mode": cfg.get("mode_name", "digital"), "preset": preset, "run_id": "digital-run",
        "session_id": 1,
        "epoch": got.epoch, "statuses": {"行为模型": "已运行", "RTL 功能协同": "未执行"},
        "recon_status": img.status, "integrity": report.status,
        "lost_blocks": len(tr.losses), "seed": cfg["seed"],
    }  # fmt: skip
    run.update(extra_run or {})
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=1), encoding="utf-8")

    digital = _digital_view(got, res, tx, tr, stages, items, cfg)
    (out / "digital.json").write_text(json.dumps(digital, ensure_ascii=False), encoding="utf-8")
    if cfg.get("mode_name") == "physical":
        (truth / "link_lineage.json").write_text(
            json.dumps(
                {"visibility": "ORACLE_ONLY", "origin": "SIMULATED", "records": digital["lineage"]}
            ),
            encoding="utf-8",
        )
    (out / "viewer.html").write_text(
        build_viewer(load_parameters(), [], run, digital), encoding="utf-8"
    )
    return run


def _digital_view(got, res, tx, tr, stages, items, cfg) -> dict[str, Any]:
    keep = np.zeros(len(got), dtype=int)
    for line in res.lines:
        a = line.start_index - got.first_index
        keep[a : a + len(line.samples)] = 1
    sync = np.zeros(len(got), dtype=int)
    for line in res.lines:
        sync[max(line.counter_zero_index - got.first_index, 0)] = 1
    lineage = []
    for line in res.lines[:3]:
        i = line.start_index - got.first_index
        item = next(x for x in items if x.line_number == line.line_number)
        path = [s.name for s in stages]
        lineage.append({
            "sample_index": line.start_index, "channel": got.channels[0],
            "sample_time": int(tx.sample_time[i]), "adc_output_time": int(tx.adc_output_time[i]),
            "pin_arrival_time": int(tx.pin_arrival_time[i]),
            "capture_time": int(tx.capture_time[i]),
            "queue_path": path if item.item_id is not None else [],
        })  # fmt: skip
    return {
        "first_index": got.first_index,
        "times": [int(x) for x in tx.sample_time],
        "lines": [
            {"line": x.line_number, "start_index": x.start_index, "count": len(x.samples)}
            for x in res.lines
        ],  # fmt: skip
        "tracks": [
            {
                "id": "sync",
                "name": "行同步（同步后计数起点）",
                "kind": "logic",
                "values": sync.tolist(),
            },
            {"id": "window", "name": "数字保留窗口", "kind": "logic", "values": keep.tolist()},
            *[
                {
                    "id": f"ch{c}",
                    "name": f"通道 {c} 码值",
                    "kind": "analog",
                    "values": [int(v) for v in got.codes[:, j]],
                }
                for j, c in enumerate(got.channels)
            ],
        ],  # fmt: skip
        "lineage": lineage,
        "queues": [
            {
                "name": s.name,
                "capacity": s.capacity_bytes,
                "peak": st.peak_bytes,
                "overflow_count": st.overflow_count,
            }
            for s, st in zip(stages, tr.stats, strict=True)
        ],  # fmt: skip
        "losses": [
            {
                "layer": x.layer,
                "reason": x.reason,
                "first": x.first_sample_index,
                "last": x.last_sample_index,
                "time_ps": x.time_ps,
            }
            for x in tr.losses
        ],  # fmt: skip
        "evidence": "测试码与队列参数均为仿真设定，不是硬件事实",
    }
