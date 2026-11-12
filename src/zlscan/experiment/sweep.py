"""Independent parameter sweeps for research presets."""

from __future__ import annotations

import csv
import itertools
import json
import math
from dataclasses import fields, is_dataclass, replace
from html import escape
from pathlib import Path
from typing import Any

from zlscan.config.presets import demo_config, nominal_config
from zlscan.engine.physical_run import run_physical

METRICS = (
    "saturated_samples",
    "front_end_clipped_samples",
    "pmt_over_limit_samples",
    "lost_blocks",
)


def _write_summary(output: Path, axes: list[str], points: list[dict]) -> None:
    columns = ["index", *axes, *METRICS, "integrity", "run"]
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for point in points:
            writer.writerow(
                {
                    **{key: point[key] for key in ("index", *METRICS, "integrity", "run")},
                    **point["parameters"],
                }
            )
    # Each panel plots one count against axis 1, one series per axis-2 value.
    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="760"',
        ' viewBox="0 0 900 760"><rect width="900" height="760" fill="white"/>',
        '<g font-family="sans-serif" font-size="12" fill="black">',
        '<text x="30" y="25">Research sweep — assumed parameters; simulation counts</text>',
    ]
    groups = (
        dict.fromkeys(p["parameters"][axes[1]] for p in points) if len(axes) == 2 else {None: None}
    )
    colors = ("#1565c0", "#c62828", "#2e7d32", "#6a1b9a", "#ad5800")
    xs = [p["parameters"][axes[0]] for p in points]
    xmin, xmax = min(xs), max(xs)
    for panel, metric in enumerate(METRICS):
        top = 50 + panel * 175
        ymax = max(1, max(p[metric] for p in points))
        svg.extend(
            [
                f'<text x="30" y="{top}">{metric} (0 … {ymax})</text>',
                f'<path d="M70 {top + 10} V{top + 120} H660" fill="none" stroke="black"/>',
                f'<text x="70" y="{top + 140}">{xmin:g}</text>',
                f'<text x="620" y="{top + 140}">{xmax:g}</text>',
                f'<text x="260" y="{top + 155}">{escape(axes[0])}</text>',
            ]
        )
        for number, value in enumerate(groups):
            selected = sorted(
                (p for p in points if len(axes) == 1 or p["parameters"][axes[1]] == value),
                key=lambda p: p["parameters"][axes[0]],
            )
            xy = [
                (
                    70 + 590 * (p["parameters"][axes[0]] - xmin) / (xmax - xmin or 1),
                    top + 120 - 105 * p[metric] / ymax,
                )
                for p in selected
            ]
            color = colors[number % len(colors)]
            coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in xy)
            svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}"/>')
            for x, y in xy:
                svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"/>')
            label = f"{axes[1]}={value:g}" if len(axes) == 2 else "series"
            svg.append(
                f'<text x="680" y="{top + 20 + number * 14}" fill="{color}">{escape(label)}</text>'
            )
    svg.append("</g></svg>")
    (output / "curves.svg").write_text("\n".join(svg) + "\n", encoding="utf-8")


def _replace_path(target: Any, parts: list[str], value: Any) -> Any:
    if not is_dataclass(target) or parts[0] not in {f.name for f in fields(target)}:
        raise ValueError(f"unknown configuration path: {'.'.join(parts)}")
    old = getattr(target, parts[0])
    if len(parts) > 1:
        value = _replace_path(old, parts[1:], value)
    elif isinstance(old, bool) or not isinstance(old, (int, float)):
        raise ValueError("sweep axes must be numeric configuration fields")
    elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("sweep values must be finite numbers")
    elif isinstance(old, int) and not isinstance(value, int):
        raise ValueError("integer configuration fields require integer values")
    return replace(target, **{parts[0]: value})


def run_sweep(
    output: Path,
    *,
    preset: str,
    parameter_values: dict[str, list[Any]],
    lines: int,
) -> dict:
    """Run every Cartesian point independently and write a machine-readable index."""
    if not isinstance(parameter_values, dict) or not 1 <= len(parameter_values) <= 2:
        raise ValueError("provide one or two parameter axes")
    if any(not isinstance(values, list) or not values for values in parameter_values.values()):
        raise ValueError("parameter_values must contain non-empty lists")
    if preset not in {"nominal", "demo"}:
        raise ValueError("preset must be nominal or demo")
    if isinstance(lines, bool) or not isinstance(lines, int) or lines < 1:
        raise ValueError("lines must be a positive integer")
    if output.exists():
        raise ValueError("sweep output already exists; choose a new directory")
    factory = nominal_config if preset == "nominal" else demo_config
    axes = list(parameter_values)
    if any(not isinstance(key, str) or key.startswith("_") for key in axes):
        raise ValueError("axis names must be configuration paths")
    configs = []
    # Validate every override before starting a run or creating any output.
    for values in itertools.product(*(parameter_values[key] for key in axes)):
        cfg = factory()
        overrides = dict(zip(axes, values, strict=True))
        for key, value in overrides.items():
            cfg = _replace_path(cfg, key.split("."), value)
        configs.append((cfg, overrides))
    points = []
    for index, (cfg, overrides) in enumerate(configs):
        point_dir = output / f"point-{index:04d}"
        result = run_physical(point_dir, cfg, lines)
        points.append({"index": index, "parameters": overrides, "run": str(point_dir), **result})
    index = {"preset": preset, "axes": parameter_values, "lines": lines, "points": points}
    output.mkdir(parents=True, exist_ok=True)
    (output / "sweep.json").write_text(
        json.dumps(index, indent=2, default=str) + "\n", encoding="utf-8"
    )
    _write_summary(output, axes, points)
    return index
