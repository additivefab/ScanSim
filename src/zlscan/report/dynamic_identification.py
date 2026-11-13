"""Export conditional dynamic residuals; fitting and held-out acquisitions stay separate."""

from pathlib import Path


def write_dynamic_residuals(result: dict, path: Path) -> None:
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise ValueError("dynamic residual plots require the zlscan[ident] extra") from exc
    with rc_context({"svg.hashsalt": "zlscan-dynamic-identification", "svg.fonttype": "none"}):
        figure = Figure(figsize=(9, 6), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots(2, 1)
        for axis, direction in zip(axes, ("rise", "fall"), strict=True):
            for row in result.get("traces", []):
                if row["direction"] != direction:
                    continue
                values = [
                    (t, y)
                    for t, y in zip(row["time_s"], row["residual_si"], strict=True)
                    if y is not None
                ]
                if values:
                    axis.scatter(
                        [x for x, _ in values],
                        [y for _, y in values],
                        s=7,
                        label=row["name"] + " / " + row["partition"],
                        c="#1769aa" if row["partition"] == "fit" else "#d66c00",
                    )
            axis.axhline(0, color="#777", linewidth=0.8)
            axis.set_xlabel("Exported time (s); acquisitions are not synchronized")
            axis.set_ylabel(f"Residual ({result.get('unit_si', 'SI')})")
            axis.set_title(
                direction
                + " / "
                + result.get("directions", {})
                .get(direction, {})
                .get("fit_curve_status", "not identified")
            )
            if axis.collections:
                axis.legend()
        figure.suptitle(
            "Observed exponential model estimates; conditional errors; no hardware acceptance",
            fontsize=10,
        )
        figure.savefig(path, metadata={"Date": None})
        figure.savefig(path.with_suffix(".png"), dpi=130, metadata={"Software": "zlscan"})
