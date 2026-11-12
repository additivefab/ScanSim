"""Benchmark the 100 × 1000 research scenario and compare both ZLRAW outputs."""

import argparse
import hashlib
import json
import platform
import sys
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from zlscan.config.presets import demo_config
from zlscan.engine.physical_run import line_steps, run_physical, stream_raw
from zlscan.evidence.records import compare, environment_info, git_state, write_record
from zlscan.io.rawstream import read_all
from zlscan.truth import scan
from zlscan.truth.scene import step_wedge

ROOT = Path(__file__).resolve().parents[1]
NX, LINES = 100, 1000


def small_frame_config():
    cfg = demo_config()
    pitch = cfg.scan.pitch_x_m
    width, height = NX * pitch, LINES * cfg.scan.pitch_y_m
    cfg.scan = scan.ScanConfig(
        facets=4,
        rpm=6000.0,
        focal_m=0.1,
        pitch_x_m=pitch,
        pitch_y_m=cfg.scan.pitch_y_m,
        nx=NX,
        x_center_m=width / 2,
    )
    cfg.scenes = {"main": step_wedge([0.2, 0.8, 1.4, 2.0, 2.6], width, height)}
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new directory for evidence and outputs")
    args = parser.parse_args()
    out = args.output
    started_at = datetime.now(UTC)
    code = git_state(ROOT)
    out.mkdir(parents=True)
    payload = out / "output"
    payload.mkdir()
    cfg = small_frame_config()
    steps = line_steps(cfg) * LINES
    data = {
        "machine": platform.platform(),
        "python": sys.version.split()[0],
        "nx": NX,
        "lines": LINES,
        "fine_steps": steps,
        "adc_samples": steps // cfg.os,
        "active_s": cfg.scan.active_s,
        "line_period_s": cfg.scan.line_period_s,
    }

    tracemalloc.start()
    t0 = time.perf_counter()
    run = run_physical(payload / "end_to_end", small_frame_config(), lines=LINES)
    wall = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    data["end_to_end"] = {
        "wall_s": wall,
        "steps_per_s": steps / wall,
        "python_peak_bytes": peak,
        "integrity": run.get("integrity"),
        "recon_status": run.get("recon_status"),
        "lost_blocks": run.get("lost_blocks"),
    }

    r = stream_raw(payload / "stream.zlraw", small_frame_config(), LINES, measure_memory=True)
    data["streaming"] = {k: r[k] for k in r if isinstance(r[k], (int, float, str, bool))}

    batch_path = payload / "end_to_end" / "observable" / "raw.zlraw"
    stream_path = payload / "stream.zlraw"
    batch, stream = read_all(batch_path), read_all(stream_path)
    same_codes = stream.blocks == batch.blocks and all(
        np.array_equal(a.samples, b.samples)
        for a, b in zip(stream.segments, batch.segments, strict=True)
    )
    criteria = [
        compare("batch integrity", "complete", run["integrity"]),
        compare("minimal reconstruction", "complete", run["recon_status"]),
        compare("lost blocks", 0, run["lost_blocks"]),
        compare("streaming lines", LINES, r["lines_written"]),
        compare("streaming fine steps", steps, r["fine_steps"]),
        compare("raw reader accepted both streams", True, not (batch.errors or stream.errors)),
        compare("raw block metadata and all samples identical", True, same_codes),
    ]
    data["criteria"] = criteria
    data["config_hash"] = cfg.hash()
    data["output_sha256"] = {}
    for path in (batch_path, stream_path):
        with path.open("rb") as handle:
            data["output_sha256"][str(path.relative_to(payload))] = hashlib.file_digest(
                handle, "sha256"
            ).hexdigest()
    result = 0 if all(criterion["passed"] for criterion in criteria) else 1
    summary = json.dumps(data, indent=2)
    (out / "summary.json").write_text(summary + "\n", encoding="utf-8")
    e, s = data["end_to_end"], data["streaming"]
    md = f"""# 阶段 3 小幅面实测（100 × 1000）

机器为 {data["machine"]}，使用 Python {data["python"]}。研究模式使用 demo 预设衍生场景，共
100 列 × 1000 行（每像素 2 mm，1 MS/s，每样本 4 细步），参数来自仿真假设。

- 细步数 {steps:.3g}，ADC 样本数 {steps // cfg.os:.3g}。
- 端到端（链 + 链路 + 采集 + 队列 + 原始文件 + 完整性 + 重建）：{e["wall_s"]:.1f} 秒，
  {e["steps_per_s"]:.3g} 细步/秒，Python 内存峰值 {e["python_peak_bytes"] / 1e6:.1f} MB；
  完整性 {e["integrity"]}，重建状态 {e["recon_status"]}，丢块 {e["lost_blocks"]}。
- 流式写盘：{s.get("wall_s", float("nan")):.1f} 秒，Python 内存峰值
  {s.get("python_peak_bytes", 0) / 1e6:.1f} MB，写出 {s.get("lines_written")} 行。

- 两路输出的块元数据和 ADC 码逐项比对，结果为 {"一致" if same_codes else "有差异"}。

本算例未设耗时阈值。4000 × 5000、7000 × 8600、双通道以及 RTL 协同未执行。
"""
    (out / "summary.md").write_text(md, encoding="utf-8")
    write_record(
        out / "record",
        name="stage3_small_frame",
        status="measured",
        conclusion="pass" if result == 0 else "fail",
        commands=[{"cmd": " ".join(sys.argv), "returncode": result}],
        inputs={"chain_config_sha256": cfg.hash()},
        seed=cfg.seed,
        criteria=criteria,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        log_text=summary,
        code=code,
        environment=environment_info(),
        notes="Single-channel research scenario. Pass applies to this scenario only.",
    )
    print(summary)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
