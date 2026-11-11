"""Small HDF5 benchmark for the trace store (S0-T10, decision D010).

Four synthetic probes of 20 million float32 samples each stand in for fine-step analogue
quantities. Per probe the store keeps the multi-level min/max summaries (zlscan.trace.pyramid)
and a 2-million-sample raw window; the rest of the run is not stored (it is recomputed from a
checkpoint, see plan section 5.14). Measured: write time, file size with and without gzip, and
the latency of time-window reads, both on a freshly opened file and on a held-open one.

Limits that are part of the result: the OS page cache is NOT dropped (that needs sudo), so
"fresh open" means a new h5py handle, not a cold disk; one machine; synthetic data; the answer
holds only for the sizes measured here.
Writes docs/reports/trace_store_benchmark.md and trace_store_benchmark_data.json.
"""

import json
import platform
import statistics
import sys
import tempfile
import time
from pathlib import Path

import h5py
import numpy as np

from zlscan.trace.pyramid import Pyramid

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "docs/reports"
PROBES = 4
N = 20_000_000
RAW_WINDOW = 2_000_000
QUERIES = 30
PIXELS = 1000
SPAN_SMALL = (1_000, 100_000, 1_000_000)  # raw read sizes
SPAN_SUMMARY = (1_000_000, 10_000_000, 20_000_000)  # spans answered from the summaries


def synthetic(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(N, dtype=np.float64)
    x = np.sin(t / 5000.0) + 0.3 * np.sin(t / 37.0) + 0.05 * rng.standard_normal(N)
    spikes = rng.integers(0, N - 4, size=50)
    for s in spikes:
        x[s : s + 3] += 8.0  # three-sample pulses a mean reduction would erase
    return x.astype(np.float32)


def write_store(path: Path, signals: list[np.ndarray], pyramids: list[Pyramid], codec) -> float:
    start = time.perf_counter()
    with h5py.File(path, "w") as f:
        for i, (x, p) in enumerate(zip(signals, pyramids, strict=True)):
            g = f.create_group(f"s{i}")
            for k, level in enumerate(p.levels):
                g.create_dataset(f"L{k}", data=level, chunks=(min(4096, len(level)), 4), **codec)
            g.create_dataset("raw_window", data=x[:RAW_WINDOW], chunks=(65536,), **codec)
    return time.perf_counter() - start


def timed(fn, *args) -> float:
    t = time.perf_counter()
    fn(*args)
    return (time.perf_counter() - t) * 1000.0


def stats(ms: list[float]) -> dict:
    s = sorted(ms)
    return {
        "median_ms": statistics.median(s),
        "p95_ms": s[int(0.95 * (len(s) - 1))],
        "max_ms": s[-1],
    }


def read_summary(f, pyr: Pyramid, probe: int, start: int, end: int):
    level = pyr.choose_level(end - start, PIXELS)
    size = pyr.block_size(level)
    return f[f"s{probe}/L{level}"][start // size : -(-end // size)]


def run_query(f, reader: str, pyramid: Pyramid, probe: int, start: int, span: int) -> None:
    if reader == "summary":
        read_summary(f, pyramid, probe, start, start + span)
    else:
        f[f"s{probe}/raw_window"][start : start + span]


def run_query_fresh(path: Path, *query) -> None:
    with h5py.File(path, "r") as f:
        run_query(f, *query)


def measure(path: Path, pyramids: list[Pyramid], rng) -> dict:
    out = {}
    for label, spans, reader in (
        ("summary", SPAN_SUMMARY, "summary"),
        ("raw_window", SPAN_SMALL, "raw"),
    ):
        for span in spans:
            hot, fresh = [], []
            with h5py.File(path, "r") as held:
                for _ in range(QUERIES):
                    probe = int(rng.integers(PROBES))
                    limit = (N if reader == "summary" else RAW_WINDOW) - span
                    start = int(rng.integers(0, limit + 1))
                    query = (reader, pyramids[probe], probe, start, span)
                    hot.append(timed(run_query, held, *query))
                    fresh.append(timed(run_query_fresh, path, *query))
            out[f"{label}_span_{span}"] = {"held_open": stats(hot), "fresh_open": stats(fresh)}
    return out


def main() -> int:
    signals = [synthetic(100 + i) for i in range(PROBES)]
    pyramids = [Pyramid.build(x) for x in signals]
    for x, p in zip(signals, pyramids, strict=True):
        assert p.levels[-1][:, 1].max() == x.max(), "the summary lost a peak"
    rng = np.random.default_rng(0)
    results = {
        "machine": {
            "platform": platform.platform(), "machine": platform.machine(),
            "python": platform.python_version(), "h5py": h5py.__version__, "numpy": np.__version__,
        },
        "data": {"probes": PROBES, "samples_per_probe": N, "raw_window": RAW_WINDOW,
                 "raw_bytes_if_stored_in_full": PROBES * N * 4,
                 "levels": [len(level) for level in pyramids[0].levels]},
        "variants": {},
    }  # fmt: skip
    with tempfile.TemporaryDirectory(prefix="zlscan_trace_") as tmp:
        for name, codec in (
            ("uncompressed", {}),
            ("gzip4", {"compression": "gzip", "compression_opts": 4}),
        ):
            path = Path(tmp) / f"{name}.h5"
            write_s = write_store(path, signals, pyramids, codec)
            results["variants"][name] = {
                "write_seconds": write_s,
                "file_bytes": path.stat().st_size,
                "queries": measure(path, pyramids, rng),
            }
    (REPORTS / "trace_store_benchmark_data.json").write_text(
        json.dumps(results, indent=1) + "\n", encoding="utf-8"
    )
    full = results["data"]["raw_bytes_if_stored_in_full"]
    lines = [
        "# 迹线存储基准（S0-T10）",
        "",
        "脚本：`tools/trace_store_benchmark.py`（可重跑）；数据：`trace_store_benchmark_data.json`。",
        "",
        "机器与版本：{platform}（{machine}），Python {python}，h5py {h5py}，NumPy {numpy}。".format(
            **results["machine"]
        ),
        "",
        "## 测的是什么",
        "",
        f"{PROBES} 路合成探针，每路 {N:,} 个 float32 样本，叠加了 50 个三样本宽的脉冲（均值降采样会抹掉它们）。"
        f"每路存多层极值摘要（`zlscan.trace.pyramid`，层大小 {results['data']['levels']}）和前 {RAW_WINDOW:,} 个样本的原始窗口；"
        "其余原始样本不存，按方案 5.14 节从检查点重算。",
        f"若全部原始样本都存，{PROBES} 路共 {full / 1e6:.0f} MB。",
        "",
        "## 文件大小与写入时间",
        "",
        "| 变体 | 文件大小 | 相对全量原始 | 写入 |",
        "|---|---:|---:|---:|",
    ]
    for name, v in results["variants"].items():
        lines.append(
            f"| {name} | {v['file_bytes'] / 1e6:.1f} MB | {v['file_bytes'] / full * 100:.1f}% | {v['write_seconds']:.2f} s |"
        )
    lines += [
        "",
        "## 按时间窗读取的延迟（毫秒，30 次随机窗口）",
        "",
        '"复用句柄"：文件已打开；"重新打开"：每次新开 h5py 句柄再读（操作系统缓存**没有**清空）。',
        "",
        "| 变体 | 查询 | 复用句柄 中位 / P95 | 重新打开 中位 / P95 |",
        "|---|---|---:|---:|",
    ]
    for name, v in results["variants"].items():
        for q, r in v["queries"].items():
            h, fr = r["held_open"], r["fresh_open"]
            lines.append(
                f"| {name} | {q} | {h['median_ms']:.2f} / {h['p95_ms']:.2f} | {fr['median_ms']:.2f} / {fr['p95_ms']:.2f} |"
            )
    lines += [
        "",
        "## 结论的范围",
        "",
        "- 只对这里测的规模成立：4 路、每路 2000 万样本、一台机器、合成数据。",
        '- 没有清空操作系统缓存（需要 sudo），所以没有真正的冷盘数据；"重新打开"只含句柄创建的开销。',
        "- 摘要层保住了全部脉冲峰值（脚本里有断言；单元测试另有覆盖）。",
        "- 是否保留 HDF5：见 DECISIONS.md D010。",
        "",
    ]
    (REPORTS / "trace_store_benchmark.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
