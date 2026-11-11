"""Stage 3 performance measurements (S3-T11, S3-T14): kernel, end-to-end, memory, checkpoints.

Everything here is MEASURED on this machine at the small demo size, then EXTRAPOLATED (not run)
to the two target frames. The extrapolation is arithmetic from the measured step rate and says
nothing about cache effects at full size. Writes docs/reports/stage3_performance.{md,json}.
"""

import json
import platform
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

from zlscan.config.presets import demo_config
from zlscan.engine.chain import Chain
from zlscan.engine.physical_run import line_steps, run_physical, stream_raw
from zlscan.replay.replay import replay_window, run_with_checkpoints

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "4000x5000 (package B shape: 50 MSPS, 1 ms line)": dict(lines=5000, fs=50_000_000, line_s=1e-3),
    "7000x8600 (package A shape: 20 MSPS, 1 ms line)": dict(lines=8600, fs=20_000_000, line_s=1e-3),
}
GOAL_WALL_S = 900  # parameter benchmark.wall_time_goal_s (an assumed goal, not a promise)


def timed(fn):
    t0 = time.perf_counter()
    r = fn()
    return r, time.perf_counter() - t0


def main() -> int:
    data: dict = {"machine": platform.platform(), "python": sys.version.split()[0]}
    cfg = demo_config()
    steps = line_steps(cfg)

    # kernel: chain only
    n_lines = 12
    _, wall = timed(lambda: Chain(cfg).run_steps(n_lines * steps, steps))
    data["kernel_noise_on"] = {"lines": n_lines, "steps": n_lines * steps, "wall_s": wall,
                               "steps_per_s": n_lines * steps / wall}  # fmt: skip
    quiet = demo_config()
    quiet.detector = type(quiet.detector)(**{**quiet.detector.__dict__, "shot_noise": False})
    _, wall_q = timed(lambda: Chain(quiet).run_steps(n_lines * steps, steps))
    data["kernel_noise_off"] = {"steps_per_s": n_lines * steps / wall_q, "wall_s": wall_q}

    # end to end: chain + link + acquisition + queues + raw file + integrity + reconstruction
    with tempfile.TemporaryDirectory() as d:
        tracemalloc.start()
        _, wall_e = timed(lambda: run_physical(Path(d), demo_config(), lines=n_lines))
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
    data["end_to_end"] = {"lines": n_lines, "wall_s": wall_e, "python_peak_bytes": peak,
                          "steps_per_s": n_lines * steps / wall_e}  # fmt: skip

    # streaming memory vs height
    with tempfile.TemporaryDirectory() as d:
        mem = {}
        for lines in (4, 16, 64):
            r = stream_raw(Path(d) / f"s{lines}.zlraw", demo_config(), lines, measure_memory=True)
            mem[lines] = {"python_peak_bytes": r["python_peak_bytes"], "wall_s": r["wall_s"]}
    data["streaming_memory_by_lines"] = mem

    # checkpoints and replay
    full, store = run_with_checkpoints(demo_config(), 8 * steps, interval_steps=steps)
    rep = replay_window(demo_config(), store, 5 * steps + steps // 2, steps // 4)
    data["checkpoints"] = {"count": len(store.blobs), "bytes_each": len(store.blobs[steps]),
                           "total_bytes": store.total_bytes, "interval": "one per line"}  # fmt: skip
    data["replay_one_quarter_line"] = {"wall_s": rep.wall_s, "warmup_steps": rep.discarded_steps}

    # extrapolation to the target frames
    rate = data["end_to_end"]["steps_per_s"]
    ex = {}
    for name, t in TARGETS.items():
        total = t["lines"] * round(t["line_s"] * t["fs"] * cfg.os)
        ex[name] = {"fine_steps": total, "hours_at_measured_rate": total / rate / 3600,
                    "goal_s": GOAL_WALL_S, "goal_met": total / rate <= GOAL_WALL_S,
                    "status": "EXTRAPOLATED, NOT RUN"}  # fmt: skip
    data["targets"] = ex
    (ROOT / "docs/reports/stage3_performance.json").write_text(
        json.dumps(data, indent=1), encoding="utf-8"
    )

    md = [
        "# 阶段 3 性能测量",
        "",
        f"机器：{data['machine']}，Python {data['python']}。小尺寸（demo 场景）实测，"
        "大幅面为**外推，没有运行**。",
        "",
        f"- 内核（只有链，噪声开）：{data['kernel_noise_on']['steps_per_s']:.3g} 细步/秒；噪声关："
        f"{data['kernel_noise_off']['steps_per_s']:.3g} 细步/秒。",
        f"- 端到端（链 + 链路 + 采集 + 队列 + 原始文件 + 完整性 + 重建）：{data['end_to_end']['steps_per_s']:.3g} 细步/秒，"
        f"Python 内存峰值 {data['end_to_end']['python_peak_bytes'] / 1e6:.1f} MB（{n_lines} 行）。",
        "- 流式写盘内存峰值随行数："
        + "，".join(f"{k} 行 {v['python_peak_bytes'] / 1e6:.2f} MB" for k, v in mem.items())
        + "。",
        f"- 检查点：每行一个，每个 {data['checkpoints']['bytes_each']} 字节；{data['checkpoints']['count']} 个共 "
        f"{data['checkpoints']['total_bytes']} 字节。",
        f"- 回放四分之一行窗口（从最近检查点预热 {rep.discarded_steps} 步）：{rep.wall_s * 1000:.0f} ms。",
        "",
        "## 目标幅面（外推，未运行）",
        "",
    ]
    for name, e in ex.items():
        md.append(
            f"- {name}：{e['fine_steps']:.3g} 细步，按实测端到端速率约 {e['hours_at_measured_rate']:.1f} 小时；"
            f"目标 {GOAL_WALL_S} 秒（假设的目标）**{'达到' if e['goal_met'] else '未达到'}**。"
        )
    md += [
        "",
        "含义：按当前实测速率，这两个幅面都跑不进目标时间（外推，未实际运行）；要达标需要更快的内核"
        "（例如编译加速）或降低细步数，这是阶段 7 的内容。本报告不声称性能目标已满足。",
        "",
    ]
    (ROOT / "docs/reports/stage3_performance.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
