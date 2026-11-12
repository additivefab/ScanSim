"""Standalone residual plots for scalar identification, using optional Matplotlib."""

from __future__ import annotations

from pathlib import Path


def write_static_residuals(result: dict, path: Path) -> None:
    """Plot fit/held-out records separately, without interpolating observations."""
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise ValueError("static residual plots require the zlscan[ident] extra") from exc
    points = [
        (role, r)
        for role, rows in [
            ("fit", result.get("fit_rows", [])),
            ("validation", result.get("validation", {}).get("rows", [])),
        ]
        for r in rows
    ]
    with rc_context({"svg.hashsalt": "zlscan-static-identification", "svg.fonttype": "none"}):
        figure = Figure(figsize=(8, 4), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots()
        for role, colour in [("fit", "#1769aa"), ("validation", "#d66c00")]:
            values = [
                (i, r["residual"])
                for i, (part, r) in enumerate(points)
                if part == role and r["residual"] is not None
            ]
            if values:
                axes.scatter(
                    [x for x, _ in values], [y for _, y in values], s=24, c=colour, label=role
                )
        axes.axhline(0, color="#777", linewidth=0.8)
        if axes.collections:
            axes.legend()
        else:
            axes.text(
                0.5,
                0.5,
                "No identifiable residuals available",
                transform=axes.transAxes,
                ha="center",
            )
        indices = list(range(0, len(points), max(1, len(points) // 8)))
        axes.set_xticks(
            indices, [points[i][1]["output_id"] for i in indices], rotation=25, ha="right"
        )
        axes.set_xlabel("Observation ID (record order; not time)")
        axes.set_ylabel(f"Residual ({result.get('output_unit_si', 'SI')})")
        axes.set_title("Conditional residuals (model estimates)")
        figure.suptitle(
            f"Data: {result.get('data_source_kind', 'unknown')}; conditional model; "
            "no hardware acceptance",
            fontsize=9,
        )
        figure.savefig(path, metadata={"Date": None})
        figure.savefig(path.with_suffix(".png"), dpi=130, metadata={"Software": "zlscan"})
