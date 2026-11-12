"""Render the assumed low-light audit without filling invalid density estimates."""

import argparse
import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def plot(report_path: Path, destination: Path):
    report = json.loads(report_path.read_text())
    fig, axes = plt.subplots(2, 2, figsize=(11, 7), constrained_layout=True)
    colors = dict(
        zip(report["specification"]["grids"], plt.cm.viridis(np.linspace(0.1, 0.9, 4)), strict=True)
    )
    for row in report["step_summaries"]:
        for ax, direction in zip(axes[0], ("down", "up"), strict=True):
            values = row["mean_waves"][direction]
            time_us = np.arange(len(values)) / report["specification"]["fs_hz"] * 1e6
            ax.plot(time_us, values, label=f"os={row['grid']}", color=colors[row["grid"]])
    axes[0, 0].set_title("Low-light down-step: 1e6 to 1e5 electrons/s")
    axes[0, 1].set_title("Low-light up-step: 1e5 to 1e6 electrons/s")
    for ax in axes[0]:
        ax.set(xlabel="Time after step (us)", ylabel="Mean quantized ADC voltage (V)")
        ax.legend(fontsize=8)
    for grid, color in colors.items():
        rows = [
            r
            for r in report["calibration_summaries"]
            if r["grid"] == grid and r["arm"] == "fixed_finest"
        ]
        x = [r["expected_density"] for r in rows]
        mean, lower, upper = [], [], []
        for row in rows:
            stat = row["bias_od_valid_only"]
            value = stat["mean"]
            mean.append(np.nan if value is None else value)
            lower.append(value - stat["ci"][0] if stat["ci"] else np.nan)
            upper.append(stat["ci"][1] - value if stat["ci"] else np.nan)
        axes[1, 0].errorbar(
            x, mean, yerr=[lower, upper], color=color, marker="o", capsize=2, label=f"os={grid}"
        )
        axes[1, 1].plot(
            x,
            [r["invalid_fraction"]["mean"] for r in rows],
            marker="o",
            color=color,
            label=f"os={grid}",
        )
    axes[1, 0].axhline(0, color="0.5", linewidth=0.7)
    axes[1, 0].set(
        title="Held-out residual: fixed finest-grid calibration",
        xlabel="Held-out reference density (OD)",
        ylabel="Valid-only bias (OD), pointwise 95% CI",
    )
    axes[1, 1].set(
        title="Invalid output fraction (point estimates)",
        xlabel="Held-out reference density (OD)",
        ylabel="Invalid fraction",
        ylim=(-0.03, 1.03),
    )
    axes[1, 1].legend(fontsize=8)
    for ax in axes.flat:
        ax.grid(alpha=0.2)
    fig.suptitle(
        "ASSUMED candidate model: numerical study, not device capability\n"
        "Missing density points remain missing; conservative fraction intervals are in the report",
        fontsize=11,
    )
    fig.savefig(destination, dpi=160)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError(args.out)
    plot(args.report, args.out)
