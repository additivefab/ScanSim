"""Standalone timestamp residuals; separate captures never form a synchronized waveform."""

from pathlib import Path


def write_timing_residuals(result: dict, path: Path) -> None:
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise ValueError("timing residual plots require the zlscan[ident] extra") from exc
    with rc_context({"svg.hashsalt": "zlscan-timing-identification", "svg.fonttype": "none"}):
        figure = Figure(figsize=(9, 6), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots(2, 1)
        curves = result.get("facet_period_fit", {}).get("fit_curves", []) or result.get(
            "global_period_fit", {}
        ).get("fit_curves", [])
        for row in curves:
            capture = next(c for c in result["captures"] if c["name"] == row["name"])
            first = int(capture["line_indices"][0])
            x = [int(i) - first for i in capture["line_indices"]]
            axes[0].scatter(x, row["residual_ps"], s=9, label=row["name"])
        for row in result.get("validation", {}).get("rows", []):
            pairs = [
                (int(p["counter_delta"]), p["residual_ps"])
                for p in row["points"]
                if p["residual_ps"] is not None
            ]
            if pairs:
                axes[1].scatter(
                    [x for x, _ in pairs], [y for _, y in pairs], s=9, label=row["name"]
                )
        for axis, title in zip(
            axes,
            (
                "Fit timestamps (intercept profiled per capture)",
                "Held-out elapsed time (first edge anchor; not scored)",
            ),
            strict=True,
        ):
            axis.axhline(0, color="#777", linewidth=0.8)
            axis.set_title(title)
            axis.set_xlabel("Observed counter delta; no missing edges invented")
            axis.set_ylabel("Residual (ps)")
            if axis.collections:
                axis.legend()
            else:
                axis.text(
                    0.5, 0.5, "No identifiable residuals", transform=axis.transAxes, ha="center"
                )
        kinds = (
            ",".join(sorted({s["source_kind"] for s in result.get("sources", [])})) or "unavailable"
        )
        figure.suptitle(
            f"{kinds}: conditional estimates; captures not synchronized; no hardware acceptance",
            fontsize=10,
        )
        figure.savefig(path, metadata={"Date": None})
        figure.savefig(path.with_suffix(".png"), dpi=130, metadata={"Software": "zlscan"})
